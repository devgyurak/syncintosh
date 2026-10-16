# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: end-to-end sync for a single text file across two machines.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
Cheap scrypt parameters are passed explicitly; the production default is never reduced (§3.1).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text import TextAdapter
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import (
    CommitmentsCorrupt,
    ContentDigestMismatch,
    GitError,
    KeyGenerationMismatch,
    PendingConflict,
    SecretScanError,
    UnknownKeyGeneration,
)
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import FileFormat
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.ports import LiveFiles, PushOutcome
from syncintosh.core.repo import COMMITMENTS_PATH, object_path
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.sync import SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.lock import FileLock
from syncintosh.system.pending import DirectoryPendingStore
from syncintosh.system.repo_files import FileRepo
from syncintosh.system.secret_scan import PatternSecretScanner
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
# The test builds commits by hand, so its pathspec is the whole repo tree.
ALL_PATHS: tuple[str, ...] = (".",)
ZSH = ".zshrc"


@dataclass
class Machine:
    home: Path
    repo: Path
    ledger: Path
    pending: Path
    lock: Path
    config_file: Path
    store: Any
    remote: str
    machine_id: str
    recovery: Path | None = None
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))

    @property
    def zshrc(self) -> Path:
        return self.home / ZSH


class StaleFirstFetchTransport(GitCLITransport):
    """A transport whose first fetch is hidden, so the remote looks behind when it is ahead.

    It reproduces a push race without a second thread: the sync fetches nothing, believes it is
    ahead, and its push loses to a commit that was already on the remote.
    """

    def __init__(self, workdir: Path) -> None:
        super().__init__(workdir)
        self._fetched = False

    def fetch(self, remote: str, branch: str) -> None:
        if self._fetched:
            super().fetch(remote, branch)
        else:
            self._fetched = True

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        self._fetched = True
        return super().push(remote, branch, commit)


def _git(repo: Path | str, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _machine(tmp_path: Path, make_store, name: str, remote: str) -> Machine:
    home = tmp_path / name / "home"
    state = home / ".local" / "share" / "syncintosh"
    return Machine(
        home=home,
        repo=state / "repo",
        ledger=state / "applied.json",
        pending=state / "pending",
        lock=state / "sync.lock",
        config_file=home / ".config" / "syncintosh" / "config.json",
        store=make_store(),
        remote=remote,
        machine_id=name,
    )


def _config_items(machine: Machine, path: str = ZSH, fmt: FileFormat = "text") -> SyncConfig:
    return SyncConfig.create(
        remote=machine.remote,
        machine_id=machine.machine_id,
        items=(LocalItem.create(path=path, preset="zsh", scope="common", format=fmt),),
    )


def _initialize(
    machine: Machine, *, import_from: Path | None = None, path: str = ZSH, fmt: FileFormat = "text"
) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    save_config(_config_items(machine, path=path, fmt=fmt), machine.config_file)
    transport = GitCLITransport(machine.repo)
    if import_from is None:
        transport.init()
        machine.recovery = machine.home / "recovery.sntr"
        init_new_repo(
            store=machine.store,
            codec=machine.codec,
            files=FileRepo(machine.repo),
            live=SystemLiveFiles(),
            recovery_path=str(machine.recovery),
            passphrase=PASSPHRASE,
        )
        return
    transport.clone(machine.remote)
    blob = transport.read_blob(transport.head() or "", COMMITMENTS_PATH)
    import_ring(
        store=machine.store,
        codec=machine.codec,
        blob=import_from.read_bytes(),
        passphrase=PASSPHRASE,
        published={} if blob is None else loads_commitments(blob),
    )


def _run(
    machine: Machine,
    *,
    adopt: AdoptChoice | None = None,
    transport: GitCLITransport | None = None,
    live: LiveFiles | None = None,
    adapters: dict | None = None,
) -> SyncReport:
    active = transport or GitCLITransport(machine.repo)
    return sync(
        transport=active,
        files=FileRepo(machine.repo),
        cipher=EnvelopeCipher(machine.store),
        scanner=PatternSecretScanner(),
        store=machine.store,
        codec=machine.codec,
        driver=TextAdapter(),
        adapters={"jsonc": JsoncAdapter()} if adapters is None else adapters,
        live=live or SystemLiveFiles(),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        adopt=adopt,
    )


def _write_live(machine: Machine, data: bytes) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    machine.zshrc.write_bytes(data)


def _write_file(machine: Machine, relpath: str, data: bytes) -> None:
    target = machine.home / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


class MidSyncEditLive(SystemLiveFiles):
    """A live store whose second read of the tracked path reports an edit.

    It reproduces a user editing the file between the collect and the apply, the window the
    compare-and-swap exists to catch.
    """

    def __init__(self, tracked: Path) -> None:
        self._tracked = str(tracked)
        self._reads = 0

    def read(self, path: str) -> bytes | None:
        if path == self._tracked:
            self._reads += 1
            if self._reads >= 2:
                return b"edited during sync\n"
        return super().read(path)


def _read_live(machine: Machine) -> bytes | None:
    return machine.zshrc.read_bytes() if machine.zshrc.exists() else None


def _pair(
    tmp_path: Path,
    make_store,
    content: bytes = b"one\ntwo\nthree\n",
    *,
    path: str = ZSH,
    fmt: FileFormat = "text",
) -> tuple[Machine, Machine, str]:
    """Two machines in sync on `content`: A published it, B cloned and applied it."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a, path=path, fmt=fmt)
    _write_file(a, path, content)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery, path=path, fmt=fmt)
    (b.home / path).parent.mkdir(parents=True, exist_ok=True)
    _run(b)
    return a, b, remote


# --------------------------------------------------------------------------- 1


def test_a_change_on_one_machine_reaches_the_other(tmp_path, memory_key_store) -> None:
    # given — two machines in sync
    a, b, _ = _pair(tmp_path, memory_key_store)

    # when — A changes the file and syncs, then B syncs
    _write_live(a, b"one changed\n")
    _run(a)
    _run(b)

    # then
    assert _read_live(b) == b"one changed\n"


# --------------------------------------------------------------------------- 2


def test_one_sided_changes_on_each_machine_both_land(tmp_path, memory_key_store) -> None:
    # given — each machine changes a different region
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"ONE\ntwo\nthree\n")
    _run(a)

    # when — B changes another region and syncs into A's published commit
    _write_live(b, b"one\ntwo\nTHREE\n")
    report = _run(b)
    _run(a)

    # then — both changes survive on both machines
    assert report.has_conflicts is False
    assert _read_live(b) == b"ONE\ntwo\nTHREE\n"
    assert _read_live(a) == b"ONE\ntwo\nTHREE\n"


# --------------------------------------------------------------------------- 3


def test_a_same_region_conflict_never_touches_a_live_file(tmp_path, memory_key_store) -> None:
    # given
    a, b, remote = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    a_commit = _git(a.repo, "rev-parse", "HEAD").strip()
    _write_live(b, b"one\nFROM B\nthree\n")

    # when
    report = _run(b)

    # then — the conflict is reported, pending is written, and B's file is untouched
    assert report.has_conflicts is True
    assert _read_live(b) == b"one\nFROM B\nthree\n"
    assert DirectoryPendingStore(b.pending).is_pending() is True
    # the other machine's commit was not deleted from the remote or from B's object store
    assert _git(Path(remote), "rev-parse", BRANCH).strip() == a_commit
    assert _git(b.repo, "cat-file", "-e", a_commit) == ""


def test_a_pending_conflict_refuses_the_next_sync(tmp_path, memory_key_store) -> None:
    # given — a stopped sync left a pending conflict
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when & then
    with pytest.raises(PendingConflict):
        _run(b)
    assert _read_live(b) == b"one\nFROM B\nthree\n"


# --------------------------------------------------------------------------- 4


def test_a_first_connect_with_different_files_asks_for_adopt(tmp_path, memory_key_store) -> None:
    # given — B has a file the repo already tracks with different content
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write_live(a, b"from A\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write_live(b, b"from B\n")

    # when — no choice made yet
    report = _run(b)

    # then — nothing is written and the file is named
    assert report.needs_adopt is True
    assert report.first_sight == (ZSH,)
    assert _read_live(b) == b"from B\n"


def test_adopting_remote_takes_the_repo_content(tmp_path, memory_key_store) -> None:
    # given
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write_live(a, b"from A\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write_live(b, b"from B\n")

    # when
    _run(b, adopt="remote")

    # then
    assert _read_live(b) == b"from A\n"


def test_adopting_local_publishes_the_local_content(tmp_path, memory_key_store) -> None:
    # given
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write_live(a, b"from A\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write_live(b, b"from B\n")

    # when
    _run(b, adopt="local")
    _run(a)

    # then — B kept its content and A adopted it
    assert _read_live(b) == b"from B\n"
    assert _read_live(a) == b"from B\n"


# --------------------------------------------------------------------------- 5


def test_a_racing_push_converges(tmp_path, memory_key_store) -> None:
    # given — A publishes while B has a change, and B's first fetch is hidden
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"ONE\ntwo\nthree\n")
    _run(a)
    _write_live(b, b"one\ntwo\nTHREE\n")

    # when — B syncs believing the remote is unchanged
    report = _run(b, transport=StaleFirstFetchTransport(b.repo))
    _run(a)

    # then — the retry merged both changes
    assert report.has_conflicts is False
    assert _read_live(b) == b"ONE\ntwo\nTHREE\n"
    assert _read_live(a) == b"ONE\ntwo\nTHREE\n"


# --------------------------------------------------------------------------- 6


def test_an_unchanged_sync_creates_no_commit_and_rewrites_nothing(tmp_path, memory_key_store) -> None:
    # given — a settled machine
    a, _, _ = _pair(tmp_path, memory_key_store)
    before_commits = _git(a.repo, "rev-list", "--count", "HEAD").strip()
    before_objects = sorted(_git(a.repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines())

    # when
    report = _run(a)

    # then
    assert report.unchanged is True
    assert _git(a.repo, "rev-list", "--count", "HEAD").strip() == before_commits
    assert sorted(_git(a.repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines()) == before_objects
    assert _git(a.repo, "status", "--porcelain") == ""


# --------------------------------------------------------------------------- 7


def test_the_repo_holds_no_plaintext_and_no_real_path(tmp_path, memory_key_store) -> None:
    # given — a machine that published a recognizable plaintext
    a = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(a)
    _write_live(a, b"export EDITOR=vim\n")
    _run(a)
    objects = _git(a.repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines()
    blobs = [line.split()[0] for line in objects if line.split()[1] == "blob"]

    # when
    needles = (b"export EDITOR=vim", b".zshrc")
    leaks = [
        needle.decode()
        for needle in needles
        for blob in blobs
        if needle in _git_bytes(a.repo, "cat-file", "blob", blob)
    ]

    # then
    assert leaks == []


# --------------------------------------------------------------------------- 8


def test_a_secret_pattern_is_refused_before_encryption(tmp_path, memory_key_store) -> None:
    # given — the tracked file carries an AWS access key id
    a = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(a)
    _write_live(a, b"AWS_ACCESS_KEY_ID=" + b"AKIA" + b"ABCDEFGHIJKLMNOP" + b"\n")

    # when & then — the scan refuses it at the encrypt boundary, so nothing is sealed
    with pytest.raises(SecretScanError):
        _run(a)
    assert FileRepo(a.repo).read("manifest.enc") is None
    assert FileRepo(a.repo).list_paths("objects") == ()


def test_a_denylisted_path_is_never_collected(tmp_path, memory_key_store) -> None:
    # given — an item pointing at a private key
    a = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    a.home.mkdir(parents=True, exist_ok=True)
    save_config(_config_items(a, path=".ssh/id_rsa"), a.config_file)
    GitCLITransport(a.repo).init()
    init_new_repo(
        store=a.store,
        codec=a.codec,
        files=FileRepo(a.repo),
        live=SystemLiveFiles(),
        recovery_path=str(a.home / "recovery.sntr"),
        passphrase=PASSPHRASE,
    )
    (a.home / ".ssh").mkdir(parents=True)
    (a.home / ".ssh" / "id_rsa").write_bytes(b"-----BEGIN " + b"RSA PRIVATE KEY-----\n")

    # when
    report = _run(a)

    # then — reported and never collected
    assert report.skipped == (".ssh/id_rsa",)
    assert FileRepo(a.repo).list_paths("objects") == ()


# --------------------------------------------------------------------------- 9


def test_a_generation_this_machine_lacks_is_reported(tmp_path, memory_key_store) -> None:
    # given — a clone whose key ring was never imported
    _, _, remote = _pair(tmp_path, memory_key_store)
    b = _machine(tmp_path, memory_key_store, "machine-c", remote)
    b.home.mkdir(parents=True, exist_ok=True)
    save_config(_config_items(b), b.config_file)
    GitCLITransport(b.repo).clone(remote)

    # when & then
    with pytest.raises(UnknownKeyGeneration):
        _run(b)


def test_a_published_commitment_mismatch_stops_the_sync(tmp_path, memory_key_store) -> None:
    # given — the remote publishes a commitment that is not this machine's key
    a, b, remote = _pair(tmp_path, memory_key_store)
    FileRepo(a.repo).write(COMMITMENTS_PATH, b'{"1":"' + b"f" * 64 + b'"}')
    transport = GitCLITransport(a.repo)
    commit = transport.commit((transport.head() or "",), "tamper", ALL_PATHS)
    transport.update_ref(BRANCH, commit)
    transport.push(remote, BRANCH, commit)

    # when & then
    with pytest.raises(KeyGenerationMismatch):
        _run(b)


def test_a_repo_that_publishes_no_commitments_is_refused(tmp_path, memory_key_store) -> None:
    # given — the remote drops keys/commitments.json, disabling the check
    a, b, remote = _pair(tmp_path, memory_key_store)
    FileRepo(a.repo).delete(COMMITMENTS_PATH)
    transport = GitCLITransport(a.repo)
    commit = transport.commit((transport.head() or "",), "drop the commitments", ALL_PATHS)
    transport.update_ref(BRANCH, commit)
    transport.push(remote, BRANCH, commit)

    # when & then
    with pytest.raises(CommitmentsCorrupt):
        _run(b)


# --------------------------------------------------------------------------- 10


def test_collected_and_applied_files_are_regular_files(tmp_path, memory_key_store) -> None:
    # given — a machine that has collected its file
    a, _, _ = _pair(tmp_path, memory_key_store)

    # when
    symlinks = [path.name for path in a.repo.rglob("*") if path.is_symlink()]

    # then — neither the repo nor the applied live file is a symlink
    assert symlinks == []
    assert a.zshrc.is_symlink() is False
    assert a.zshrc.is_file() is True


# --------------------------------------------------------------------------- edge behaviour


def test_a_locally_removed_file_is_suppressed_not_recreated(tmp_path, memory_key_store) -> None:
    # given — B deletes its file, which is not a deletion of the record
    a, b, _ = _pair(tmp_path, memory_key_store)
    b.zshrc.unlink()

    # when
    report = _run(b)
    again = _run(b)

    # then — reported, left absent, and not recreated on the next sync
    assert report.suppressed == (ZSH,)
    assert again.suppressed == (ZSH,)
    assert b.zshrc.exists() is False
    assert _read_live(a) is not None


def test_a_tracked_path_that_does_not_exist_here_is_deferred(tmp_path, memory_key_store) -> None:
    # given — an item this machine has never created a file for
    a = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(a)

    # when
    report = _run(a)

    # then
    assert report.deferred == (ZSH,)


def test_a_record_whose_directory_is_absent_is_deferred(tmp_path, memory_key_store) -> None:
    # given — the app that owns the path is not installed on B
    remote = _bare_remote(tmp_path)
    path = "Config/app/settings.txt"
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, path=path)
    _write_file(a, path, b"set\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery, path=path)

    # when
    report = _run(b)

    # then
    assert report.deferred == (path,)
    assert (b.home / "Config").exists() is False


def test_a_symlinked_live_file_is_never_written(tmp_path, memory_key_store) -> None:
    # given — B's tracked file is a symlink to dotfiles, and A publishes a change
    a, b, _ = _pair(tmp_path, memory_key_store)
    target = b.home / "dotfiles" / "zshrc"
    _write_file(b, "dotfiles/zshrc", b"one\ntwo\nthree\n")
    b.zshrc.unlink()
    b.zshrc.symlink_to(target)
    _write_live(a, b"one\ntwo\nTHREE\n")
    _run(a)

    # when — the record is now pending-apply, so apply would normally write the file
    report = _run(b)

    # then — reported, and neither the link nor its target is touched
    assert report.symlinks == (ZSH,)
    assert b.zshrc.is_symlink() is True
    assert target.read_bytes() == b"one\ntwo\nthree\n"


def test_an_edit_during_the_sync_aborts_the_write(tmp_path, memory_key_store) -> None:
    # given — B edits its file, and the file appears to change again before apply
    _, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(b, b"local edit\n")

    # when
    report = _run(b, live=MidSyncEditLive(b.zshrc))

    # then — the write is aborted and the live file is left as the user left it
    assert report.aborted == (ZSH,)
    assert _read_live(b) == b"local edit\n"


def test_a_push_the_remote_refuses_is_an_error(tmp_path, memory_key_store) -> None:
    # given — the remote rejects every push
    a, _, remote = _pair(tmp_path, memory_key_store)
    hook = Path(remote) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _write_live(a, b"changed\n")

    # when & then
    with pytest.raises(GitError):
        _run(a)


def test_a_record_this_machine_does_not_track_is_never_written(tmp_path, memory_key_store) -> None:
    # given — B stops tracking .zshrc while the repo still holds its record
    _, b, _ = _pair(tmp_path, memory_key_store)
    save_config(_config_items(b, path=".zshenv"), b.config_file)
    _write_live(b, b"unaccounted\n")

    # when
    report = _run(b)

    # then — the record is refused and B's file is left alone
    assert report.refused == (ZSH,)
    assert _read_live(b) == b"unaccounted\n"


def test_an_apply_path_that_escapes_home_is_never_written(tmp_path, memory_key_store) -> None:
    # given — the record's parent directory is a symlink out of B's home
    remote = _bare_remote(tmp_path)
    path = "Config/app/settings.txt"
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, path=path)
    _write_file(a, path, b"set\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery, path=path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (b.home / "Config").symlink_to(outside)

    # when
    report = _run(b)

    # then — refused, and nothing landed outside the home tree
    assert report.refused == (path,)
    assert sorted(p.name for p in outside.iterdir()) == []


def test_a_replayed_object_that_mismatches_its_record_is_refused(tmp_path, memory_key_store) -> None:
    # given — A publishes v2, then its object is replaced by a valid ciphertext of other content
    a, b, remote = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\ntwo\nTHREE\n")
    _run(a)
    cipher = EnvelopeCipher(a.store)
    stored = FileRepo(a.repo).read("manifest.enc")
    assert stored is not None
    file_id = next(iter(loads_manifest(cipher.open(stored, domain="manifest", file_id="")).records))
    FileRepo(a.repo).write(object_path(file_id), cipher.seal(b"replayed\n", domain="object", file_id=file_id, key_id=1))
    transport = GitCLITransport(a.repo)
    commit = transport.commit((transport.head() or "",), "replace the object", ALL_PATHS)
    transport.update_ref(BRANCH, commit)
    transport.push(remote, BRANCH, commit)

    # when & then — B applies and detects the mix-and-match before writing
    with pytest.raises(ContentDigestMismatch):
        _run(b)
    assert _read_live(b) == b"one\ntwo\nthree\n"


def test_a_local_edit_over_a_stale_baseline_is_merged_not_pushed(tmp_path, memory_key_store) -> None:
    # given — A publishes a change while B's apply aborts, leaving a stale ledger baseline
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"ONE\ntwo\nthree\n")
    _run(a)
    aborted = _run(b, live=MidSyncEditLive(b.zshrc))

    # when — B edits the content it last saw and syncs normally
    assert aborted.aborted == (ZSH,)
    _write_live(b, b"one\ntwo\nTHREE\n")
    report = _run(b)
    _run(a)

    # then — HEAD's change is merged, not silently overwritten
    assert report.has_conflicts is False
    assert _read_live(b) == b"ONE\ntwo\nTHREE\n"
    assert _read_live(a) == b"ONE\ntwo\nTHREE\n"


def test_a_stale_baseline_conflict_stops_the_sync(tmp_path, memory_key_store) -> None:
    # given — the same stale split, but both sides changed the same region
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _run(b, live=MidSyncEditLive(b.zshrc))

    # when — B edits the region A changed
    _write_live(b, b"one\nFROM B\nthree\n")
    report = _run(b)

    # then — reported, and B's file is untouched
    assert report.has_conflicts is True
    assert _read_live(b) == b"one\nFROM B\nthree\n"


def test_a_symlinked_tracked_file_does_not_repeat_first_sight(tmp_path, memory_key_store) -> None:
    # given — B's tracked file is a symlink into a dotfiles tree
    _, b, _ = _pair(tmp_path, memory_key_store)
    target = b.home / "dotfiles" / "zshrc"
    _write_file(b, "dotfiles/zshrc", b"one\ntwo\nthree\n")
    b.zshrc.unlink()
    b.zshrc.symlink_to(target)

    # when — the link's target changes
    _write_file(b, "dotfiles/zshrc", b"edited\n")
    first = _run(b)
    second = _run(b)

    # then — reported as a link, and never as a first-sight prompt that blocks every sync
    assert first.symlinks == (ZSH,)
    assert first.needs_adopt is False
    assert second.needs_adopt is False


def test_adopting_remote_still_checks_for_an_edit_during_the_sync(tmp_path, memory_key_store) -> None:
    # given — B has a first-sight file and edits it while the sync runs
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write_live(a, b"from A\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write_live(b, b"from B\n")

    # when
    report = _run(b, adopt="remote", live=MidSyncEditLive(b.zshrc))

    # then — the edit is not clobbered (docs/design/01-sync-model.md, "every write is checked")
    assert report.aborted == (ZSH,)
    assert _read_live(b) == b"from B\n"


def test_junk_and_leftovers_in_the_repo_are_not_committed(tmp_path, memory_key_store) -> None:
    # given — OS junk and a stray object a failed run could have left
    a, _, _ = _pair(tmp_path, memory_key_store)
    (a.repo / ".DS_Store").write_bytes(b"junk")
    (a.repo / "objects" / "leftover.enc").write_bytes(b"stale")
    _write_live(a, b"one changed\n")

    # when
    _run(a)

    # then — neither is in the committed tree
    tracked = _git(a.repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".DS_Store" not in tracked
    assert "objects/leftover.enc" not in tracked


def test_a_deleted_repo_file_in_the_worktree_is_not_a_committed_deletion(tmp_path, memory_key_store) -> None:
    # given — a file vanished from the working tree, as a partial failure can leave it
    a, _, _ = _pair(tmp_path, memory_key_store)
    (a.repo / "keys" / "commitments.json").unlink()
    _write_live(a, b"one changed\n")

    # when
    _run(a)

    # then — the next cycle restores it rather than committing its deletion
    tracked = _git(a.repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "keys/commitments.json" in tracked


def test_a_live_file_that_already_matches_head_is_not_committed_again(tmp_path, memory_key_store) -> None:
    # given — a ledger rewound to a stale baseline while the live file already matches HEAD
    a, _, _ = _pair(tmp_path, memory_key_store)
    stale = dict(JsonLedgerStore(a.ledger).load())
    _write_live(a, b"one changed\n")
    _run(a)
    JsonLedgerStore(a.ledger).save(stale)
    commits = _git(a.repo, "rev-list", "--count", "HEAD").strip()

    # when
    report = _run(a)

    # then — no empty commit (the write plan, not the collect list, decides)
    assert report.unchanged is True
    assert _git(a.repo, "rev-list", "--count", "HEAD").strip() == commits


def test_a_remote_whose_head_names_a_missing_branch_is_adopted(tmp_path, memory_key_store) -> None:
    # given — a bare remote from plain `git init --bare`, whose HEAD names `master`
    remote = tmp_path / "legacy.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    a = _machine(tmp_path, memory_key_store, "machine-a", str(remote))
    _initialize(a)
    _write_live(a, b"one\ntwo\nthree\n")
    _run(a)

    # when — a second machine clones a remote whose HEAD names a branch it does not have
    b = _machine(tmp_path, memory_key_store, "machine-b", str(remote))
    _initialize(b, import_from=a.recovery)
    report = _run(b)

    # then — B is on the branch that exists and applies A's content
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\ntwo\nthree\n"


def test_the_ledger_holds_no_plaintext_and_no_real_path(tmp_path, memory_key_store) -> None:
    # given
    a, _, _ = _pair(tmp_path, memory_key_store)

    # when
    text = a.ledger.read_text(encoding="utf-8")

    # then — the ledger is digests only (AGENTS.md §3.3)
    assert "one" not in text
    assert ZSH not in text


# --------------------------------------------------------------------------- jsonc


JSONC = "Library/Application Support/Code/User/settings.json"
JSONC_BASE = '{\n  // editor\n  "editor.fontSize": 13,\n  "editor.tabSize": 4\n}\n'


def _read_jsonc(machine: Machine) -> str:
    return (machine.home / JSONC).read_text(encoding="utf-8")


def test_jsonc_different_keys_merge_and_comments_survive(tmp_path, memory_key_store) -> None:
    # given — two machines in sync on a commented settings file
    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")

    # when — each changes a different key
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)
    (b.home / JSONC).write_text(JSONC_BASE.replace('"editor.tabSize": 4', '"editor.tabSize": 2'), encoding="utf-8")
    report = _run(b)
    _run(a)

    # then — both changes land, the comment is kept, and nothing conflicted
    assert report.has_conflicts is False
    merged = _read_jsonc(b)
    assert '"editor.fontSize": 15' in merged
    assert '"editor.tabSize": 2' in merged
    assert "// editor" in merged


def test_jsonc_same_key_on_both_sides_stops_the_sync(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)

    # when — B edits the same key A changed
    local = JSONC_BASE.replace("13", "12")
    (b.home / JSONC).write_text(local, encoding="utf-8")
    report = _run(b)

    # then — the conflict names the key path and never its values, and B's file is untouched
    assert report.has_conflicts is True
    assert report.conflicts[0].detail.endswith("editor.fontSize")
    assert "15" not in report.conflicts[0].detail
    assert _read_jsonc(b) == local


def test_jsonc_that_does_not_parse_is_a_conflict(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)

    # when — B's file is malformed (no closing brace)
    bad = '{\n  "editor.fontSize": 12\n'
    (b.home / JSONC).write_text(bad, encoding="utf-8")
    report = _run(b)

    # then
    assert report.has_conflicts is True
    assert _read_jsonc(b) == bad
