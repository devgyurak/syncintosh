# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: end-to-end sync for a single text file across two machines.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
Cheap scrypt parameters are passed explicitly; the production default is never reduced (§3.1).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import dumps_commitments, loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import (
    CommitmentsCorrupt,
    ConflictsNotCovered,
    ContentDigestMismatch,
    GitError,
    KeyGenerationMismatch,
    PendingConflict,
    PruneRefused,
    SecretScanError,
    SyntaxInvalid,
    UnknownKeyGeneration,
)
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import MANIFEST_SCHEMA, FileFormat, Manifest
from syncintosh.core.manifest_codec import dumps_manifest, loads_manifest
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import ContentAdapter, LiveFiles, PresetCatalog, PushOutcome, SyntaxValidator
from syncintosh.core.presets import EMPTY_CATALOG, PresetFile, ValidatorName
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, object_path, wrap_path
from syncintosh.core.resolution import Decide, Proposal
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.diff import DiffReport, diff_repo
from syncintosh.services.keys import (
    export_ring,
    import_ring,
    init_new_repo,
    prune_generation,
    remote_commitments,
    rotate_repo,
)
from syncintosh.services.resolve import decision, scoped_chooser, take_chooser
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


def _config_for_paths(machine: Machine, paths: tuple[str, ...]) -> SyncConfig:
    """A config that tracks several text files, all owned by the zsh preset."""
    items = tuple(LocalItem.create(path=path, preset="zsh", scope="common", format="text") for path in paths)
    return SyncConfig.create(remote=machine.remote, machine_id=machine.machine_id, items=items)


def _initialize(
    machine: Machine,
    *,
    import_from: Path | None = None,
    path: str = ZSH,
    fmt: FileFormat = "text",
    paths: tuple[str, ...] | None = None,
) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    config = _config_items(machine, path=path, fmt=fmt) if paths is None else _config_for_paths(machine, paths)
    save_config(config, machine.config_file)
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
    resolving: bool = False,
    decide: Decide | None = None,
    catalog: PresetCatalog | None = None,
    validators: Mapping[ValidatorName, SyntaxValidator] | None = None,
) -> SyncReport:
    active = transport or GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    return sync(
        transport=active,
        files=FileRepo(machine.repo),
        cipher=cipher,
        scanner=PatternSecretScanner(),
        store=machine.store,
        codec=machine.codec,
        wrapper=cipher,
        driver=TextMergeDriver(),
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
        resolving=resolving,
        decide=decide,
        catalog=EMPTY_CATALOG if catalog is None else catalog,
        validators={} if validators is None else validators,
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


def test_a_rotation_published_on_one_machine_is_absorbed_by_another(tmp_path, memory_key_store) -> None:
    # given — A and B in sync under generation 1, then A publishes a hygiene rotation
    a, b, _ = _pair(tmp_path, memory_key_store)
    key2 = bytes(range(32, 64))
    _rotate_by_hand(a, new_key=key2)

    # when — B syncs and fetches the rotation, which carries keys/2.wrap
    report = _run(b)

    # then — B absorbed generation 2 and applied the re-sealed content
    assert b.store.generations() == (1, 2)
    assert b.store.get(2) == key2
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\ntwo\nthree\n"


def test_a_wrap_whose_key_does_not_match_the_commitment_is_refused(tmp_path, memory_key_store) -> None:
    # given — A rotates, but the published commitment for generation 2 is not the wrapped key
    a, b, remote = _pair(tmp_path, memory_key_store)
    _rotate_by_hand(a, new_key=bytes(range(32, 64)))
    key1 = a.store.get(1)
    assert key1 is not None
    FileRepo(a.repo).write(COMMITMENTS_PATH, dumps_commitments({1: a.codec.commitment(key1), 2: "f" * 64}))
    transport = GitCLITransport(a.repo)
    commit = transport.commit((transport.head() or "",), "tamper the commitment", ALL_PATHS)
    transport.update_ref(BRANCH, commit)
    transport.push(remote, BRANCH, commit)

    # when & then — the wrap is checked against the commitment before it reaches B's ring
    with pytest.raises(KeyGenerationMismatch):
        _run(b)
    assert b.store.get(2) is None


def _rotate_by_hand(machine: Machine, *, new_key: bytes) -> None:
    """Re-seal every object and the manifest under a freshly minted generation, and publish it.

    PR B implements this as `key rotate`; here it is built from the rotation-slice primitives so
    the fetch-absorption path is exercised before the command exists. A hygiene rotation adds the
    new key to the ring, publishes `keys/2.wrap` (KG_2 under KG_1), re-seals every object and the
    manifest under KG_2, updates the commitments, and commits once
    (``docs/design/05-key-generations.md``).
    """
    files = FileRepo(machine.repo)
    transport = GitCLITransport(machine.repo)
    head = transport.head()
    assert head is not None
    key1 = machine.store.get(1)
    assert key1 is not None
    cipher = EnvelopeCipher(machine.store)
    stored_manifest = files.read(MANIFEST_PATH)
    assert stored_manifest is not None
    manifest = loads_manifest(cipher.open(stored_manifest, domain="manifest", file_id=""))
    machine.store.put(2, new_key)
    files.write(wrap_path(2), cipher.wrap(new_key, key_id=2, under_id=1))
    for file_id in manifest.records:
        stored = files.read(object_path(file_id))
        assert stored is not None
        plaintext = cipher.open(stored, domain="object", file_id=file_id)
        files.write(object_path(file_id), cipher.seal(plaintext, domain="object", file_id=file_id, key_id=2))
    rotated = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=2, records=manifest.records)
    files.write(MANIFEST_PATH, cipher.seal(dumps_manifest(rotated), domain="manifest", file_id="", key_id=2))
    files.write(
        COMMITMENTS_PATH, dumps_commitments({1: machine.codec.commitment(key1), 2: machine.codec.commitment(new_key)})
    )
    commit = transport.commit((head,), "syncintosh: rotate", ALL_PATHS)
    transport.update_ref(BRANCH, commit)
    transport.push(machine.remote, BRANCH, commit)


# --------------------------------------------------------------------------- key rotate


def _rotate(
    machine: Machine,
    *,
    out_of_band: bool = False,
    transport: GitCLITransport | None = None,
    scanner=None,
) -> int:
    active = transport or GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    return rotate_repo(
        transport=active,
        files=FileRepo(machine.repo),
        cipher=cipher,
        wrapper=cipher,
        scanner=PatternSecretScanner() if scanner is None else scanner,
        store=machine.store,
        codec=machine.codec,
        lock=FileLock(machine.lock, timeout_seconds=5),
        pending=DirectoryPendingStore(machine.pending),
        remote=machine.remote,
        branch=active.branch() or BRANCH,
        mode="out-of-band" if out_of_band else "hygiene",
    )


def _only_file_id(machine: Machine, rev: str = "HEAD") -> str:
    blob = GitCLITransport(machine.repo).read_blob(rev, MANIFEST_PATH)
    assert blob is not None
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(blob, domain="manifest", file_id=""))
    return next(iter(manifest.records))


def _object_generation(machine: Machine, rev: str, file_id: str) -> int:
    blob = GitCLITransport(machine.repo).read_blob(rev, object_path(file_id))
    assert blob is not None
    return int.from_bytes(blob[1:5], "big")


def test_a_hygiene_rotation_reseals_everything_and_reaches_the_other_machine(tmp_path, memory_key_store) -> None:
    # given — A and B in sync under generation 1
    a, b, _ = _pair(tmp_path, memory_key_store)
    before = _git(a.repo, "rev-parse", "HEAD").strip()
    file_id = _only_file_id(a)

    # when — A rotates
    generation = _rotate(a)

    # then — HEAD is bound to generation 2, while the old commit still decrypts under generation 1
    assert generation == 2
    assert _object_generation(a, "HEAD", file_id) == 2
    assert _object_generation(a, before, file_id) == 1

    # and — B absorbs the wrap and applies the re-sealed content
    _run(b)
    assert b.store.get(2) == a.store.get(2)
    assert _read_live(b) == b"one\ntwo\nthree\n"


def test_a_rotation_that_cannot_fast_forward_is_discarded_and_retryable(tmp_path, memory_key_store) -> None:
    # given — B publishes a change A has not fetched
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(b, b"from B\n")
    _run(b)
    before = _git(a.repo, "rev-parse", "HEAD").strip()

    # when — A rotates without seeing B's commit, so its push is not a fast-forward
    with pytest.raises(GitError):
        _rotate(a, transport=StaleFirstFetchTransport(a.repo))

    # then — the rotation is discarded: no new generation, and the branch is restored
    assert a.store.generations() == (1,)
    assert _git(a.repo, "rev-parse", "HEAD").strip() == before

    # and — a retry that fetches normally succeeds
    assert _rotate(a) == 2


def test_a_rotation_whose_push_is_rejected_is_discarded_and_retryable(tmp_path, memory_key_store) -> None:
    # given — the remote rejects every push
    a, b, remote = _pair(tmp_path, memory_key_store)
    hook = Path(remote) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    # when & then — the rotation is discarded, leaving the ring as it was
    with pytest.raises(GitError):
        _rotate(a)
    assert a.store.generations() == (1,)

    # when the remote accepts again — the re-run succeeds and B converges
    hook.unlink()
    assert _rotate(a) == 2
    _run(b)
    assert b.store.get(2) == a.store.get(2)
    assert _read_live(b) == b"one\ntwo\nthree\n"


def test_an_out_of_band_rotation_publishes_no_wrap_and_needs_an_import(tmp_path, memory_key_store) -> None:
    # given — A and B in sync
    a, b, _ = _pair(tmp_path, memory_key_store)

    # when — A rotates out of band
    assert _rotate(a, out_of_band=True) == 2

    # then — with no keys/2.wrap published, B cannot absorb the new generation
    with pytest.raises(UnknownKeyGeneration):
        _run(b)

    # when — A exports the ring and B imports it out of band
    exported = a.home / "ring.sntr"
    export_ring(store=a.store, codec=a.codec, live=SystemLiveFiles(), path=str(exported), passphrase=PASSPHRASE)
    import_ring(
        store=b.store,
        codec=b.codec,
        blob=exported.read_bytes(),
        passphrase=PASSPHRASE,
        published=remote_commitments(GitCLITransport(b.repo), b.remote, BRANCH),
    )

    # then — B holds generation 2 and applies the re-sealed content
    assert b.store.get(2) == a.store.get(2)
    report = _run(b)
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\ntwo\nthree\n"


def test_rotate_on_a_machine_behind_a_rotation_absorbs_it_first(tmp_path, memory_key_store) -> None:
    # given — A rotated while B was offline
    a, b, _ = _pair(tmp_path, memory_key_store)
    _rotate(a)

    # when — B rotates without having synced the rotation
    generation = _rotate(b)

    # then — B absorbed generation 2 first and rotated on top of it
    assert generation == 3
    assert b.store.get(2) == a.store.get(2)


def test_rotate_refuses_before_moving_the_branch_when_it_lacks_the_remote_generation(
    tmp_path, memory_key_store
) -> None:
    # given — A rotated out of band, so there is no wrap for B to absorb
    a, b, _ = _pair(tmp_path, memory_key_store)
    _rotate(a, out_of_band=True)
    before = _git(b.repo, "rev-parse", "HEAD").strip()

    # when & then — B cannot read the remote manifest, so it refuses without moving the branch
    with pytest.raises(UnknownKeyGeneration):
        _rotate(b)
    assert _git(b.repo, "rev-parse", "HEAD").strip() == before
    assert b.store.generations() == (1,)


def test_a_rotation_that_fails_before_the_push_drops_the_generation(tmp_path, memory_key_store) -> None:
    # given — the re-seal's secret scan refuses
    a, _, _ = _pair(tmp_path, memory_key_store)

    class RefusingScanner:
        def scan(self, plaintext: bytes, *, subject: str, format: str) -> None:
            raise SecretScanError("refused")

    # when & then — the not-yet-published generation is taken back out of the ring
    with pytest.raises(SecretScanError):
        _rotate(a, scanner=RefusingScanner())
    assert a.store.generations() == (1,)

    # and — a normal retry succeeds
    assert _rotate(a) == 2
    assert a.store.generations() == (1, 2)


class _LyingPushTransport(GitCLITransport):
    """A transport whose push lands on the remote but is reported as rejected.

    It reproduces a lost response: the server applied the ref, the client never heard so.
    """

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        super().push(remote, branch, commit)
        return "rejected"


def test_a_push_that_lands_but_reports_failure_keeps_the_generation(tmp_path, memory_key_store) -> None:
    # given — A and B in sync, and a transport whose push lands but reports a failure
    a, _, _ = _pair(tmp_path, memory_key_store)

    # when — an out-of-band rotation is "rejected" although it reached the remote
    generation = _rotate(a, out_of_band=True, transport=_LyingPushTransport(a.repo))

    # then — the new generation is kept (it is the only copy) and the remote holds the rotation
    assert generation == 2
    assert a.store.get(2) is not None
    assert _git(Path(a.remote), "rev-parse", BRANCH).strip() == _git(a.repo, "rev-parse", "HEAD").strip()


class _RaisingPushTransport(GitCLITransport):
    """A transport whose push lands on the remote and then raises, as an interrupt can."""

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        super().push(remote, branch, commit)
        raise GitError("interrupted after the server applied it")


def test_a_push_that_raises_after_landing_keeps_the_generation(tmp_path, memory_key_store) -> None:
    # given — A and B in sync, and a transport whose push lands but then raises
    a, _, _ = _pair(tmp_path, memory_key_store)

    # when — an out-of-band rotation's push raises although it reached the remote
    generation = _rotate(a, out_of_band=True, transport=_RaisingPushTransport(a.repo))

    # then — the new generation is kept (it is the only copy) and the remote holds the rotation
    assert generation == 2
    assert a.store.get(2) is not None


def test_a_rotation_preserves_a_commitment_for_a_generation_this_machine_does_not_hold(
    tmp_path, memory_key_store
) -> None:
    # given — the repo publishes a commitment for a generation this machine never held
    a, _, remote = _pair(tmp_path, memory_key_store)
    files = FileRepo(a.repo)
    stored = files.read(COMMITMENTS_PATH)
    assert stored is not None
    files.write(COMMITMENTS_PATH, dumps_commitments({**loads_commitments(stored), 9: "a" * 64}))
    transport = GitCLITransport(a.repo)
    seed = transport.commit((transport.head() or "",), "publish an extra commitment", ALL_PATHS)
    transport.update_ref(BRANCH, seed)
    transport.push(remote, BRANCH, seed)

    # when
    _rotate(a)

    # then — the extra commitment survives
    after = files.read(COMMITMENTS_PATH)
    assert after is not None
    assert loads_commitments(after).get(9) == "a" * 64


def test_a_machine_behind_a_rotation_recovers_on_sync(tmp_path, memory_key_store) -> None:
    # given — A rotated while B had an unsynced local edit
    a, b, _ = _pair(tmp_path, memory_key_store)
    _rotate(a)
    _write_live(b, b"one\nB EDIT\nthree\n")

    # when — B syncs
    report = _run(b)

    # then — B adopted generation 2 and published its edit on it; no divergence error
    assert report.has_conflicts is False
    assert b.store.get(2) == a.store.get(2)
    assert _read_live(b) == b"one\nB EDIT\nthree\n"
    _run(a)
    assert _read_live(a) == b"one\nB EDIT\nthree\n"


def test_rotate_adopts_the_remote_when_a_stale_local_commit_diverged(tmp_path, memory_key_store) -> None:
    # given — A rotated, and B has a local commit still on the retired generation
    a, b, _ = _pair(tmp_path, memory_key_store)
    _rotate(a)
    transport = GitCLITransport(b.repo)
    stray = transport.commit((transport.head() or "",), "local", ALL_PATHS)
    transport.update_ref(BRANCH, stray)

    # when — B rotates
    generation = _rotate(b)

    # then — B adopted generation 2 and rotated on top of it
    assert generation == 3
    assert b.store.get(2) == a.store.get(2)


def test_rotate_refuses_while_a_conflict_is_pending(tmp_path, memory_key_store) -> None:
    # given — B is stopped on an unresolved conflict
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when & then — a rotation would rewind the collect commit the pending state points at
    with pytest.raises(PendingConflict):
        _rotate(b)


def test_rotate_refuses_to_rewind_an_unpushed_collect_commit(tmp_path, memory_key_store) -> None:
    # given — A has a collect commit it could not push
    a, _, remote = _pair(tmp_path, memory_key_store)
    hook = Path(remote) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _write_live(a, b"changed\n")
    with pytest.raises(GitError):
        _run(a)
    before = _git(a.repo, "rev-parse", "HEAD").strip()

    # when & then — rotate refuses rather than silently rewinding the commit
    with pytest.raises(GitError):
        _rotate(a)
    assert _git(a.repo, "rev-parse", "HEAD").strip() == before


# --------------------------------------------------------------------------- key prune


def _prune(machine: Machine, generation: int, *, transport: GitCLITransport | None = None) -> None:
    active = transport or GitCLITransport(machine.repo)
    prune_generation(
        transport=active,
        store=machine.store,
        lock=FileLock(machine.lock, timeout_seconds=5),
        remote=machine.remote,
        branch=active.branch() or BRANCH,
        generation=generation,
    )


def test_prune_refuses_a_generation_a_reachable_object_uses(tmp_path, memory_key_store) -> None:
    # given — A rotated, so HEAD's objects are encrypted under generation 2
    a, _, _ = _pair(tmp_path, memory_key_store)
    _rotate(a)

    # when & then — reachable history still needs generation 2
    with pytest.raises(PruneRefused):
        _prune(a, 2)
    assert a.store.get(2) is not None


def test_prune_refuses_the_master_generation(tmp_path, memory_key_store) -> None:
    # given — a ring without generation 1 could not be exported for recovery
    a, _, _ = _pair(tmp_path, memory_key_store)

    # when & then
    with pytest.raises(PruneRefused):
        _prune(a, 1)
    assert a.store.get(1) is not None


def test_prune_drops_a_generation_no_reachable_object_uses(tmp_path, memory_key_store) -> None:
    # given — a stray generation a crashed rotation left behind
    a, _, _ = _pair(tmp_path, memory_key_store)
    a.store.put(2, bytes(range(32, 64)))

    # when
    _prune(a, 2)

    # then
    assert a.store.generations() == (1,)


# --------------------------------------------------------------------------- diff


def _diff(machine: Machine) -> DiffReport:
    return diff_repo(
        transport=GitCLITransport(machine.repo),
        cipher=EnvelopeCipher(machine.store),
        live=SystemLiveFiles(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        deny=SystemDenyList(),
        branch=BRANCH,
        catalog=EMPTY_CATALOG,
        adapters={},
        validators={},
    )


def test_diff_reports_an_in_sync_file(tmp_path, memory_key_store) -> None:
    # given — two machines in sync
    a, _, _ = _pair(tmp_path, memory_key_store)

    # when
    report = _diff(a)

    # then
    assert [(entry.path, entry.status) for entry in report.entries] == [(ZSH, "in-sync")]


def test_diff_reports_a_local_change_without_exposing_it_through_key_paths(tmp_path, memory_key_store) -> None:
    # given — the live file differs from what the repo holds
    a, _, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nCHANGED\nthree\n")

    # when
    report = _diff(a)

    # then — reported as changed, with no key paths for a whole-file (text) format
    entry = next(entry for entry in report.entries if entry.path == ZSH)
    assert entry.status == "changed"
    assert entry.key_paths == ()


def test_diff_compares_the_live_file_with_the_fetched_remote_tip(tmp_path, memory_key_store) -> None:
    # given — A published a change; B fetched it but never applied it
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"from A\n")
    _run(a)
    GitCLITransport(b.repo).fetch(b.remote, BRANCH)

    # when
    report = _diff(b)

    # then — the diff names the repo side as the fetched commit and both plaintexts
    entry = next(entry for entry in report.entries if entry.path == ZSH)
    assert entry.status == "changed"
    assert entry.repo == "from A\n"
    assert entry.here == "one\ntwo\nthree\n"


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
    assert [deferral.path for deferral in report.deferred] == [ZSH]


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
    assert [deferral.path for deferral in report.deferred] == [path]
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

    # then — the record is left alone and reported as one this machine does not track (which is
    # ordinary, not a failure), and B's file is untouched
    assert [refusal.path for refusal in report.refused] == [ZSH]
    assert report.refused[0].blocking is False
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
    assert [refusal.path for refusal in report.refused] == [path]
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
    # the label mirrors the file: a flat dotted key is quoted, a nested pair is not
    assert report.conflicts[0].detail.endswith('"editor.fontSize"')
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


# --------------------------------------------------------------------------- resolve


class FailingPushTransport(GitCLITransport):
    """A transport whose push is refused as a non-fast-forward race that never settles.

    The resolve run merges and then pushes; a push that loses leaves the pending state in place.
    """

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        return "rejected"


def test_resolve_take_remote_settles_the_conflict_and_both_machines_converge(tmp_path, memory_key_store) -> None:
    # given — B's edit to the same line as A's is pending
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when
    report = _run(b, resolving=True, decide=decision(take_chooser("remote")))

    # then — the remote's line is the outcome, B's live file follows, and nothing stays pending
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\nFROM A\nthree\n"
    assert DirectoryPendingStore(b.pending).is_pending() is False
    assert _run(a).has_conflicts is False
    assert _read_live(a) == b"one\nFROM A\nthree\n"


def test_resolve_take_local_publishes_the_local_edit(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when
    report = _run(b, resolving=True, decide=decision(take_chooser("local")))

    # then — B's edit wins everywhere, including on A
    assert report.has_conflicts is False
    assert _run(a).has_conflicts is False
    assert _read_live(a) == b"one\nFROM B\nthree\n"


def test_a_pick_whose_fingerprint_no_longer_matches_leaves_the_conflict_pending(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when — the decision was made against some other conflict
    report = _run(b, resolving=True, decide=lambda views, answered: {"stale-fingerprint": "remote"})

    # then — the conflict that the stale pick did not match stays, and the state stays pending
    assert report.has_conflicts is True
    assert DirectoryPendingStore(b.pending).is_pending() is True
    assert _read_live(b) == b"one\nFROM B\nthree\n"


def test_a_declined_approval_publishes_nothing(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when
    report = _run(b, resolving=True, decide=decision(take_chooser("remote"), approve=lambda views, picks: False))

    # then — the conflict is still there, nothing was written to the live file, and the pending holds
    assert report.has_conflicts is True
    assert DirectoryPendingStore(b.pending).is_pending() is True
    assert _read_live(b) == b"one\nFROM B\nthree\n"


def test_resolve_with_nothing_pending_is_refused(tmp_path, memory_key_store) -> None:
    # given — a converged pair with no pending state
    _, b, _ = _pair(tmp_path, memory_key_store)

    # when & then
    with pytest.raises(PendingConflict):
        _run(b, resolving=True, decide=decision(take_chooser("remote")))


def test_a_push_that_loses_keeps_the_pending_state(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when & then — the merge cannot be published, so the conflict must still be settleable
    with pytest.raises(GitError):
        _run(b, transport=FailingPushTransport(b.repo), resolving=True, decide=decision(take_chooser("remote")))
    assert DirectoryPendingStore(b.pending).is_pending() is True


def test_a_scoped_resolve_that_names_no_conflict_publishes_nothing(tmp_path, memory_key_store) -> None:
    # given — B is stopped on a conflict on .zshrc
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when — the run is scoped to another file, so the conflict has no decision
    with pytest.raises(ConflictsNotCovered):
        _run(b, resolving=True, decide=decision(scoped_chooser((".gitconfig",), (), home=str(b.home))))

    # then — nothing is written, and the conflict is still pending
    assert _read_live(b) == b"one\nFROM B\nthree\n"
    assert DirectoryPendingStore(b.pending).is_pending() is True


def test_a_scoped_resolve_naming_the_conflict_settles_it(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when
    report = _run(b, resolving=True, decide=decision(scoped_chooser((".zshrc",), (), home=str(b.home))))

    # then
    assert report.has_conflicts is False
    assert _run(a).has_conflicts is False
    assert _read_live(a) == b"one\nFROM B\nthree\n"


def test_a_decision_is_asked_again_for_a_conflict_that_was_not_yet_answered(tmp_path, memory_key_store) -> None:
    # given — two keys conflict in one settings file; the first decision answers only the first key
    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15").replace("4\n", "5\n"), encoding="utf-8")
    _run(a)
    (b.home / JSONC).write_text(JSONC_BASE.replace("13", "12").replace("4\n", "6\n"), encoding="utf-8")
    _run(b)
    asked: list[int] = []

    def first_key_only(views, answered):
        pending = [view for view in views if view.fingerprint not in answered]
        asked.append(len(pending))
        return {pending[0].fingerprint: "remote"} if pending else None

    # when
    report = _run(b, resolving=True, decide=first_key_only)

    # then — the second round asks about the key the first round left open, and both keys settle
    assert report.has_conflicts is False
    assert asked == [2, 1]
    assert '"editor.fontSize": 15' in _read_jsonc(b)
    assert '"editor.tabSize": 5' in _read_jsonc(b)


# --------------------------------------------------------------------------- resolve across files

PROFILE = ".zprofile"
TRACKED = (ZSH, PROFILE)


def _two_file_pair(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """Two machines in sync on two tracked files: A published both, B cloned and applied them."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a, paths=TRACKED)
    _write_file(a, ZSH, b"one\ntwo\nthree\n")
    _write_file(a, PROFILE, b"alpha\nbeta\n")
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery, paths=TRACKED)
    _run(b)
    return a, b


def test_one_resolve_settles_a_stale_and_a_diverged_conflict_in_turn(tmp_path, memory_key_store) -> None:
    # given — ZSH is stale on B (its apply was aborted), PROFILE is diverged between the machines
    a, b = _two_file_pair(tmp_path, memory_key_store)
    _write_file(a, ZSH, b"one\nFROM A\nthree\n")
    _run(a)
    _run(b, live=MidSyncEditLive(b.zshrc))
    _write_file(b, ZSH, b"one\nFROM B\nthree\n")
    _write_file(a, PROFILE, b"alpha\nFROM A\n")
    _run(a)
    _write_file(b, PROFILE, b"alpha\nFROM B\n")
    stopped = _run(b)
    asked: list[list[str]] = []
    take_remote = decision(take_chooser("remote"))

    def counting(views, answered):
        asked.append(sorted(view.path for view in views if view.fingerprint not in answered))
        return take_remote(views, answered)

    # when — one resolve run answers the stale conflict, then the diverged one it exposes
    report = _run(b, resolving=True, decide=counting)

    # then — each conflict was asked in its own round, both took the remote side, and A converges
    assert stopped.has_conflicts is True
    assert asked == [[ZSH], [PROFILE]]
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\nFROM A\nthree\n"
    assert (b.home / PROFILE).read_bytes() == b"alpha\nFROM A\n"
    assert _run(a).has_conflicts is False
    assert (a.home / PROFILE).read_bytes() == b"alpha\nFROM A\n"


def test_local_and_remote_paths_settle_each_file_to_its_own_side(tmp_path, memory_key_store) -> None:
    # given — both tracked files conflict on both machines
    a, b = _two_file_pair(tmp_path, memory_key_store)
    _write_file(a, ZSH, b"one\nFROM A\nthree\n")
    _write_file(a, PROFILE, b"alpha\nFROM A\n")
    _run(a)
    _write_file(b, ZSH, b"one\nFROM B\nthree\n")
    _write_file(b, PROFILE, b"alpha\nFROM B\n")
    _run(b)

    # when & then — naming only one of the two conflicts leaves the other without a choice: nothing moves,
    # not the live files, not the pending state, and not the remote branch
    remote_before = _git(b.remote, "rev-parse", BRANCH)
    with pytest.raises(ConflictsNotCovered):
        _run(b, resolving=True, decide=decision(scoped_chooser((ZSH,), (), home=str(b.home))))
    assert DirectoryPendingStore(b.pending).is_pending() is True
    assert _read_live(b) == b"one\nFROM B\nthree\n"
    assert (b.home / PROFILE).read_bytes() == b"alpha\nFROM B\n"
    assert _git(b.remote, "rev-parse", BRANCH) == remote_before

    # when — each file is named for the side it should take
    report = _run(
        b,
        resolving=True,
        decide=decision(scoped_chooser((ZSH,), (PROFILE,), home=str(b.home))),
    )

    # then — B keeps its own edit of the first file and takes A's edit of the second, and A converges
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\nFROM B\nthree\n"
    assert (b.home / PROFILE).read_bytes() == b"alpha\nFROM A\n"
    assert _run(a).has_conflicts is False
    assert _read_live(a) == b"one\nFROM B\nthree\n"
    assert (a.home / PROFILE).read_bytes() == b"alpha\nFROM A\n"


# --------------------------------------------------------------------------- syntax fallback

BASE_JSON = '{\n  "a": "0",\n  "b": "0"\n}\n'
VALIDATED_POLICY = PresetFile.create(path=JSONC, format="jsonc", validator="json")


class _CrossKeyValidator:
    """Refuses the one document a key-level pick can build from two valid sides: a = "R" with b = "B".

    Neither side holds that pair, so each side passes and only a merged result can fail.
    """

    def check(self, text: str, *, subject: str) -> None:
        document = json.loads(text)
        if document.get("a") == "R" and document.get("b") == "B":
            raise SyntaxInvalid(f"{subject}: a and b name a combination no side holds")


class _ValidatedCatalog:
    """The built-in catalog plus one JSON file whose syntax policy the test controls."""

    def __init__(self, policy: PresetFile) -> None:
        self._policy = policy
        self._base = BuiltinCatalog.default()

    def _owns(self, path: str) -> bool:
        return collision_key(path) == collision_key(self._policy.path)

    def policy_for(self, path: str) -> PresetFile | None:
        return self._policy if self._owns(path) else self._base.policy_for(path)

    def knows(self, preset: str, path: str) -> bool:
        return True if self._owns(path) else self._base.knows(preset, path)

    def get(self, name: str):
        return self._base.get(name)

    def preset_for(self, path: str):
        return self._base.preset_for(path)


def test_a_key_pick_that_fails_the_syntax_check_is_settled_at_file_level(tmp_path, memory_key_store) -> None:
    # given — A sets a to "R"; B sets a to "L" and b to "B". Each side is valid; a key-level pick of "R"
    # for a builds {a: "R", b: "B"}, which the validator refuses.
    catalog = _ValidatedCatalog(VALIDATED_POLICY)
    validators: dict[ValidatorName, SyntaxValidator] = {"json": _CrossKeyValidator()}
    a, b, _ = _pair(tmp_path, memory_key_store, BASE_JSON.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text('{"a": "R", "b": "0"}\n', encoding="utf-8")
    _run(a, catalog=catalog, validators=validators)
    (b.home / JSONC).write_text('{"a": "L", "b": "B"}\n', encoding="utf-8")
    _run(b, catalog=catalog, validators=validators)
    asked: list[list[str]] = []
    choose_key_then_file = decision(lambda view: "remote" if view.kind == "key" else "local")

    def counting(views, answered):
        asked.append(sorted(view.kind for view in views if view.fingerprint not in answered))
        return choose_key_then_file(views, answered)

    # when
    report = _run(b, catalog=catalog, validators=validators, resolving=True, decide=counting)

    # then — the key pick is refused by the check, the file is offered next, and its local side publishes
    assert asked == [["key"], ["file"]]
    assert report.has_conflicts is False
    assert json.loads((b.home / JSONC).read_text(encoding="utf-8")) == {"a": "L", "b": "B"}
    _run(a, catalog=catalog, validators=validators)
    assert json.loads((a.home / JSONC).read_text(encoding="utf-8")) == {"a": "L", "b": "B"}


def test_a_proposed_file_settles_the_conflict_and_both_machines_converge(tmp_path, memory_key_store) -> None:
    # given — B's edit to the same line as A's is pending
    a, b, _ = _pair(tmp_path, memory_key_store)
    _write_live(a, b"one\nFROM A\nthree\n")
    _run(a)
    _write_live(b, b"one\nFROM B\nthree\n")
    _run(b)

    # when — the proposal is a line neither machine wrote
    report = _run(b, resolving=True, decide=decision(lambda view: Proposal("one\nMERGED\nthree\n")))

    # then
    assert report.has_conflicts is False
    assert _read_live(b) == b"one\nMERGED\nthree\n"
    assert DirectoryPendingStore(b.pending).is_pending() is False
    assert _run(a).has_conflicts is False
    assert _read_live(a) == b"one\nMERGED\nthree\n"


def test_a_proposed_key_value_settles_the_conflict_and_both_machines_converge(tmp_path, memory_key_store) -> None:
    # given — A and B changed the same key to different values
    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)
    (b.home / JSONC).write_text(JSONC_BASE.replace("13", "14"), encoding="utf-8")
    _run(b)

    # when — a value neither machine wrote, proposed for the conflicted key
    report = _run(b, resolving=True, decide=decision(lambda view: Proposal("16") if view.kind == "key" else None))

    # then — the comments and key order survive, and both machines hold the proposed value
    assert report.has_conflicts is False
    assert _read_jsonc(b) == JSONC_BASE.replace("13", "16")
    assert _run(a).has_conflicts is False
    assert _read_jsonc(a) == JSONC_BASE.replace("13", "16")


def test_a_proposed_key_value_that_does_not_parse_leaves_the_conflict_pending(tmp_path, memory_key_store) -> None:
    # given
    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)
    (b.home / JSONC).write_text(JSONC_BASE.replace("13", "14"), encoding="utf-8")
    _run(b)

    # when — an unclosed string is not a value this format can hold
    report = _run(b, resolving=True, decide=decision(lambda view: Proposal('"unclosed')))

    # then — the proposal is refused, the conflict stays pending, and B's file is untouched
    assert report.has_conflicts is True
    assert DirectoryPendingStore(b.pending).is_pending() is True
    assert _read_jsonc(b) == JSONC_BASE.replace("13", "14")


def test_a_proposed_key_value_that_fails_the_syntax_check_is_settled_at_file_level(tmp_path, memory_key_store) -> None:
    # given — the same cross-key setup as the key pick that fails the syntax check
    catalog = _ValidatedCatalog(VALIDATED_POLICY)
    validators: dict[ValidatorName, SyntaxValidator] = {"json": _CrossKeyValidator()}
    a, b, _ = _pair(tmp_path, memory_key_store, BASE_JSON.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text('{"a": "R", "b": "0"}\n', encoding="utf-8")
    _run(a, catalog=catalog, validators=validators)
    (b.home / JSONC).write_text('{"a": "L", "b": "B"}\n', encoding="utf-8")
    _run(b, catalog=catalog, validators=validators)
    asked: list[list[str]] = []
    propose_then_file = decision(lambda view: Proposal('"R"') if view.kind == "key" else "local")

    def counting(views, answered):
        asked.append(sorted(view.kind for view in views if view.fingerprint not in answered))
        return propose_then_file(views, answered)

    # when
    report = _run(b, catalog=catalog, validators=validators, resolving=True, decide=counting)

    # then — the proposed value is refused by the check, the file is offered next, and its local side publishes
    assert asked == [["key"], ["file"]]
    assert report.has_conflicts is False
    assert json.loads((b.home / JSONC).read_text(encoding="utf-8")) == {"a": "L", "b": "B"}
    _run(a, catalog=catalog, validators=validators)
    assert json.loads((a.home / JSONC).read_text(encoding="utf-8")) == {"a": "L", "b": "B"}


# --------------------------------------------------------------------------- restore point


def _object_at(machine: Machine, rev: str, file_id: str) -> bytes:
    blob = GitCLITransport(machine.repo).read_blob(rev, object_path(file_id))
    assert blob is not None
    return EnvelopeCipher(machine.store).open(blob, domain="object", file_id=file_id)


def test_an_apply_reports_the_restore_point_its_overwrites_come_from(tmp_path, memory_key_store) -> None:
    # given — A edits line one and B edits line three of one file, so B's sync merges and overwrites its live file
    a, b, _ = _pair(tmp_path, memory_key_store)
    before = _git(b.repo, "rev-parse", "HEAD").strip()
    _write_live(a, b"ONE\ntwo\nthree\n")
    _run(a)
    _write_live(b, b"one\ntwo\nTHREE\n")

    # when
    report = _run(b)

    # then — the restore point is the collect commit B made on the HEAD it started from, and it holds
    # the bytes the apply replaced
    assert report.applied == (ZSH,)
    assert report.backup_commit is not None
    assert _git(b.repo, "rev-parse", f"{report.backup_commit}^").strip() == before
    assert _object_at(b, report.backup_commit, _only_file_id(b, report.backup_commit)) == b"one\ntwo\nTHREE\n"
    assert _read_live(b) == b"ONE\ntwo\nTHREE\n"


# --------------------------------------------------------------------------- assisted resolve


class _ReplyAdvisor:
    """A stand-in model: one canned answer for every conflict, and a record of what it was sent."""

    def __init__(self, advice) -> None:
        self.advice = advice
        self.sent: list[str] = []

    def advise(self, index, *, path, key, base, local, remote):
        self.sent.extend([path.text, key.text, base.text, local.text, remote.text])
        return self.advice


def _assisted(b: Machine, advisor, *, accept: bool):
    from syncintosh.services.resolve import advised_chooser, proposal_problem
    from syncintosh.system.masking import TextMasker

    catalog = BuiltinCatalog.default()
    adapters: dict[FileFormat, ContentAdapter] = {"jsonc": JsoncAdapter()}

    def check(view, text):
        return proposal_problem(view, text, catalog=catalog, adapters=adapters, validators={})

    return advised_chooser(
        advisor=advisor,
        masker=TextMasker(home=str(b.home), username="integration", internal_domains=()),
        fallback=take_chooser("local"),
        present=lambda suggestion: accept,
        auto_apply=lambda view: False,
        check=check,
    )


def test_an_accepted_proposal_settles_the_conflict_and_both_machines_converge(tmp_path, memory_key_store) -> None:
    # given — A and B changed the same key to different values, and the model proposes a third
    from syncintosh.core.advice import Advice

    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)
    (b.home / JSONC).write_text(JSONC_BASE.replace("13", "14"), encoding="utf-8")
    _run(b)
    advisor = _ReplyAdvisor(Advice(choice="proposal", value="16", rationale="a third value"))

    # when
    report = _run(b, resolving=True, decide=decision(_assisted(b, advisor, accept=True)))

    # then — the proposal is published and A converges on it; the model saw masked text only
    assert report.has_conflicts is False
    assert _read_jsonc(b) == JSONC_BASE.replace("13", "16")
    assert _run(a).has_conflicts is False
    assert _read_jsonc(a) == JSONC_BASE.replace("13", "16")
    assert all(str(b.home) not in text for text in advisor.sent)


def test_a_suggestion_that_breaks_the_syntax_is_never_applied(tmp_path, memory_key_store) -> None:
    # given — the model's value does not parse, so the check refuses it before anything is applied
    from syncintosh.core.advice import Advice

    a, b, _ = _pair(tmp_path, memory_key_store, JSONC_BASE.encode("utf-8"), path=JSONC, fmt="jsonc")
    (a.home / JSONC).write_text(JSONC_BASE.replace("13", "15"), encoding="utf-8")
    _run(a)
    (b.home / JSONC).write_text(JSONC_BASE.replace("13", "14"), encoding="utf-8")
    _run(b)
    advisor = _ReplyAdvisor(Advice(choice="proposal", value='"unclosed', rationale="broken"))

    # when — the person accepts nothing the check did not offer, so the fallback side settles it
    report = _run(b, resolving=True, decide=decision(_assisted(b, advisor, accept=True)))

    # then — the broken value never reaches either file; the fallback's local side is what publishes
    assert report.has_conflicts is False
    assert _read_jsonc(b) == JSONC_BASE.replace("13", "14")
    assert _run(a).has_conflicts is False
    assert _read_jsonc(a) == JSONC_BASE.replace("13", "14")
    assert '"unclosed' not in _read_jsonc(a)
