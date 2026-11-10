# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: append-only session files across two Macs (issue #56): idle, extension, fork, secret, caps, open.

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The preset is TEST-ONLY
(`agentdemo`); the session store and the activity probe are fakes, because the real ones arrive with the agent
presets. Every transcript's content is invented.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.policy import policy_digest
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.core.sessions import IDLE_SECONDS, MIB, SessionFile, SessionListing, SessionUnit
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
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
ROOT = ".agentdemo/sessions"
SESSION_PATH = re.compile(rf"{re.escape(ROOT)}/(?P<id>s[0-9]+)(?:\.jsonl|/.+)")
# Which files a session may hold; the store lists the ones that exist on disk.
FILES = {sid: (f"{ROOT}/{sid}.jsonl", f"{ROOT}/{sid}/subagents/agent-1.jsonl") for sid in ("s1", "s2", "s3")}
AGENTDEMO = Preset.create(
    name="agentdemo",
    files=tuple(
        PresetFile.create(path=path, format="text", append_only=True) for paths in FILES.values() for path in paths
    ),
)


@dataclass(frozen=True)
class _TestCatalog(BuiltinCatalog):
    """The shipped lookup over the test preset, with the one-entry policy history a shipped preset has."""

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        preset = self.get(name)
        return None if preset is None else (policy_digest(preset),)


CATALOG = _TestCatalog.create((AGENTDEMO,))
LINE_1 = b'{"type":"user","text":"hello"}\n'
LINE_2 = b'{"type":"assistant","text":"hi"}\n'
LINE_3 = b'{"type":"user","text":"more"}\n'
SECRET_LINE = b'{"type":"tool","out":"' + b"AKIA" + b"ABCDEFGHIJKLMNOP" + b'"}\n'


class FakeStore:
    """A `SessionStore` over the files that exist under one home, with sizes reported as told."""

    def __init__(self, home: Path, sizes: dict[str, int] | None = None) -> None:
        self._home = home
        self._sizes = {} if sizes is None else sizes

    def sessions(self) -> SessionListing:
        units = []
        for sid, paths in FILES.items():
            held = [
                SessionFile.create(
                    path=path,
                    live=path,
                    size=self._sizes.get(sid, (self._home / path).stat().st_size),
                    mtime=(self._home / path).stat().st_mtime,
                )
                for path in paths
                if (self._home / path).is_file()
            ]
            if held:
                units.append(SessionUnit.create(session_id=sid, project="demo", files=held))
        return SessionListing(units=tuple(units))

    def session_of(self, path: str) -> str | None:
        found = SESSION_PATH.fullmatch(path)
        return None if found is None else found.group("id")

    def note_for(self, path: str, data: bytes) -> str | None:
        return None


class FakeActivity:
    """A `SessionActivity` that answers what it is told; a session it was not told about is closed."""

    def __init__(self, answers: dict[str, bool | None] | None = None) -> None:
        self.answers = {} if answers is None else answers

    def is_open(self, session_id: str) -> bool | None:
        return self.answers.get(session_id, False)


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
    activity: FakeActivity = field(default_factory=FakeActivity)
    sizes: dict[str, int] = field(default_factory=dict)
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))


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


def _initialize(machine: Machine, *, import_from: Path | None = None) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    base = new_machine_config(remote=machine.remote, machine_id=machine.machine_id, preset_names=(), catalog=CATALOG)
    # The allowlist: this machine tracks every session path the preset owns. The store, not the items, says which exist.
    items = tuple(
        LocalItem.create(path=path, preset="agentdemo", scope="common", format="text")
        for paths in FILES.values()
        for path in paths
    )
    from dataclasses import replace

    save_config(replace(base, items=items), machine.config_file)
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


def _run(machine: Machine, *, store: FakeStore | None = None) -> SyncReport:
    active = GitCLITransport(machine.repo)
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
        adapters={},
        catalog=CATALOG,
        live=SystemLiveFiles(),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        apps=NO_APP_INFO,
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        sessions=FakeStore(machine.home, machine.sizes) if store is None else store,
        activity=machine.activity,
    )


def _write(machine: Machine, path: str, data: bytes, *, idle: bool = True) -> Path:
    target = machine.home / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    age = IDLE_SECONDS * 4 if idle else 5
    stamp = time.time() - age
    os.utime(target, (stamp, stamp))
    return target


def _grow(machine: Machine, sid: str, *, age: int) -> None:
    """Give session `sid` both its files, each reported as 48 MiB, last written `age` idle windows ago."""
    machine.sizes[sid] = 48 * MIB
    stamp = time.time() - IDLE_SECONDS * age
    for path in FILES[sid]:
        os.utime(_write(machine, path, LINE_1), (stamp, stamp))


def _records(machine: Machine) -> list[str]:
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return sorted(record.path for record in manifest.records.values())


def _pair(tmp_path: Path, make_store):
    """A is initialised, B imports its ring; neither has a session yet."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    return a, b


def _synced_pair(tmp_path: Path, make_store):
    """A has published one idle session; B has it too, byte for byte."""
    a, b = _pair(tmp_path, make_store)
    _write(a, FILES["s1"][0], LINE_1 + LINE_2)
    _run(a)
    _initialize(b, import_from=a.recovery)
    _run(b)
    return a, b


MAIN = FILES["s1"][0]
SUB = FILES["s1"][1]


# --------------------------------------------------------------------------- idle and open


def test_an_idle_session_is_collected_and_written_on_the_other_mac(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2)
    _write(a, SUB, LINE_3)
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then — every file of the session travelled, and nothing was reported as left out
    assert (b.home / MAIN).read_bytes() == LINE_1 + LINE_2
    assert (b.home / SUB).read_bytes() == LINE_3
    assert _records(a) == sorted([MAIN, SUB])
    assert report.session_skipped == ()


def test_a_session_written_a_moment_ago_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1, idle=False)

    # when
    report = _run(a)

    # then
    assert (_records(a), [(s.session_id, s.reason) for s in report.session_skipped]) == ([], [("s1", "open")])


@pytest.mark.parametrize("answer", [True, None])
def test_an_old_session_the_agent_has_open_or_cannot_be_told_about_is_not_collected(
    tmp_path, memory_key_store, answer: bool | None
) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    a.activity.answers["s1"] = answer
    _write(a, MAIN, LINE_1)

    # when
    report = _run(a)

    # then
    assert (_records(a), [s.reason for s in report.session_skipped]) == ([], ["open"])


def test_one_young_file_keeps_the_whole_session_back(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _write(a, SUB, LINE_2, idle=False)

    # when
    _run(a)

    # then
    assert _records(a) == []


def test_a_session_the_agent_has_open_here_is_not_written_over(tmp_path, memory_key_store) -> None:
    # given — A extends and publishes; B has the old prefix but its agent has the session open
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2 + LINE_3)
    _run(a)
    b.activity.answers["s1"] = True

    # when
    report = _run(b)

    # then
    assert ((b.home / MAIN).read_bytes(), [s.reason for s in report.session_skipped]) == (LINE_1 + LINE_2, ["open"])


# --------------------------------------------------------------------------- extension and truncation


def test_a_longer_copy_on_the_repo_extends_the_local_one_and_gets_a_fresh_mtime(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    old = time.time() - IDLE_SECONDS * 4
    os.utime(b.home / MAIN, (old, old))
    _write(a, MAIN, LINE_1 + LINE_2 + LINE_3)
    _run(a)
    before = time.time()

    # when
    report = _run(b)

    # then — written without a restore point, and the new mtime is now so the agent's cleanup does not reap it
    assert (b.home / MAIN).read_bytes() == LINE_1 + LINE_2 + LINE_3
    assert (report.restore_points, report.applied) == ((), (MAIN,))
    assert (b.home / MAIN).stat().st_mtime >= before - 1 > old


def test_a_local_extension_is_published_and_reaches_the_other_mac(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(b, MAIN, LINE_1 + LINE_2 + LINE_3)

    # when
    _run(b)
    _run(a)

    # then
    assert (a.home / MAIN).read_bytes() == LINE_1 + LINE_2 + LINE_3


def test_a_longer_local_copy_is_never_shortened(tmp_path, memory_key_store) -> None:
    # given — B's session is open (so it is not collected) and longer than what the repo holds
    _, b = _synced_pair(tmp_path, memory_key_store)
    _write(b, MAIN, LINE_1 + LINE_2 + LINE_3)
    b.activity.answers["s1"] = True

    # when
    report = _run(b)

    # then
    assert (b.home / MAIN).read_bytes() == LINE_1 + LINE_2 + LINE_3
    assert report.applied == ()


def test_a_session_removed_here_is_not_recreated(tmp_path, memory_key_store) -> None:
    # given
    _, b = _synced_pair(tmp_path, memory_key_store)
    (b.home / MAIN).unlink()

    # when
    _run(b)
    _run(b)

    # then
    assert (b.home / MAIN).exists() is False


# --------------------------------------------------------------------------- continued on both Macs


def test_a_session_continued_on_both_macs_keeps_the_repos_copy_and_says_so(tmp_path, memory_key_store) -> None:
    # given — both extend the same prefix differently; A publishes first
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2 + b'{"from":"a"}\n')
    _run(a)
    _write(b, MAIN, LINE_1 + LINE_2 + b'{"from":"b"}\n')

    # when
    report = _run(b)

    # then — not a conflict; B's file is untouched, and the repo still holds A's continuation
    assert report.has_conflicts is False
    assert (b.home / MAIN).read_bytes() == LINE_1 + LINE_2 + b'{"from":"b"}\n'
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [("s1", "forked")]
    _run(a)
    assert (a.home / MAIN).read_bytes() == LINE_1 + LINE_2 + b'{"from":"a"}\n'


def test_a_fork_found_against_the_local_copy_of_the_repo_is_reported_without_collecting(
    tmp_path, memory_key_store
) -> None:
    # given — B's repo already has A's continuation; B's file diverges from it
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2 + b'{"from":"a"}\n')
    _run(a)
    _run(b)
    _write(b, MAIN, LINE_1 + LINE_2 + b'{"from":"b"}\n')
    before = _records(b)

    # when
    report = _run(b)

    # then
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [("s1", "forked")]
    assert (report.collected, _records(b)) == ((), before)


# --------------------------------------------------------------------------- secrets


def test_a_secret_in_one_file_leaves_the_whole_session_out_and_the_rest_goes_on(tmp_path, memory_key_store) -> None:
    # given — s1's sub-agent file carries a key; s2 is clean
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, FILES["s1"][0], LINE_1)
    _write(a, FILES["s1"][1], SECRET_LINE)
    _write(a, FILES["s2"][0], LINE_2)

    # when
    report = _run(a)

    # then — nothing of s1 was sealed, s2 was, and the report names the session, file and line but not the value
    assert _records(a) == [FILES["s2"][0]]
    assert [(s.session_id, s.reason, s.path) for s in report.session_skipped] == [("s1", "secret", FILES["s1"][1])]
    assert "line 1" in report.session_skipped[0].detail
    assert b"ABCDEFGHIJKLMNOP".decode() not in repr(report)


def test_a_refused_session_is_not_in_any_object_of_the_repo(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, FILES["s1"][0], SECRET_LINE)

    # when
    _run(a)

    # then
    assert [p.name for p in (a.repo / "objects").glob("*")] == []


# --------------------------------------------------------------------------- caps


def test_a_file_over_the_cap_is_skipped_and_reported(tmp_path, memory_key_store) -> None:
    # given — the store reports 49 MiB for s1; the bytes are not read
    a, _ = _pair(tmp_path, memory_key_store)
    a.sizes["s1"] = 49 * MIB
    _write(a, FILES["s1"][0], LINE_1)
    _write(a, FILES["s2"][0], LINE_2)

    # when
    report = _run(a)

    # then
    assert _records(a) == [FILES["s2"][0]]
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [("s1", "cap")]


def test_the_newest_session_of_a_project_over_the_cap_is_held_back(tmp_path, memory_key_store) -> None:
    # given — three sessions of two 48 MiB files each (96 MiB): only two fit under 256 MiB
    a, _ = _pair(tmp_path, memory_key_store)
    _grow(a, "s1", age=10)
    _grow(a, "s2", age=9)
    _grow(a, "s3", age=8)

    # when
    report = _run(a)

    # then
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [("s3", "cap")]
    assert _records(a) == sorted([*FILES["s1"], *FILES["s2"]])


# --------------------------------------------------------------------------- no store, no collection


def test_a_session_file_is_never_collected_through_the_plain_item_path(tmp_path, memory_key_store) -> None:
    # given — the item is in the config, but the store says the session is open
    a, _ = _pair(tmp_path, memory_key_store)
    a.activity.answers["s1"] = True
    _write(a, MAIN, LINE_1)

    # when
    report = _run(a)

    # then — the idle check could not be skipped by listing the path as an item
    assert (report.collected, report.created, _records(a)) == ((), (), [])


# --------------------------------------------------------------------------- a store that disagrees with the resolver


class _MisplacingStore(FakeStore):
    """A store that reports every file living somewhere other than where the resolver says."""

    def sessions(self) -> SessionListing:
        listing = super().sessions()
        moved = tuple(
            SessionUnit.create(
                session_id=unit.session_id,
                project=unit.project,
                files=tuple(
                    SessionFile.create(path=f.path, live=f"elsewhere/{f.live}", size=f.size, mtime=f.mtime)
                    for f in unit.files
                ),
            )
            for unit in listing.units
        )
        return SessionListing(units=moved)


class _UnclaimingStore(FakeStore):
    """A store that lists sessions but claims no record path as one of its own."""

    def session_of(self, path: str) -> str | None:
        return None


def test_a_file_the_store_places_differently_from_the_resolver_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _write(a, f"elsewhere/{MAIN}", LINE_1)

    # when
    report = _run(a, store=_MisplacingStore(a.home))

    # then
    assert (_records(a), [(s.session_id, s.reason) for s in report.session_skipped]) == ([], [("s1", "unknown")])


def test_a_record_no_store_claims_is_not_written_because_nothing_says_it_is_closed(tmp_path, memory_key_store) -> None:
    # given — A publishes a session; B's store claims no path
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b, store=_UnclaimingStore(b.home))

    # then
    assert ((b.home / MAIN).exists(), [s.reason for s in report.session_skipped]) == (False, ["open"])


def test_a_published_session_that_is_open_still_counts_toward_the_project_cap(tmp_path, memory_key_store) -> None:
    # given — s1 and s2 (96 MiB each) are published; then s1 is open while a third 96 MiB session appears
    a, _ = _pair(tmp_path, memory_key_store)
    _grow(a, "s1", age=10)
    _grow(a, "s2", age=9)
    _run(a)
    _grow(a, "s3", age=8)
    a.activity.answers["s1"] = True

    # when
    report = _run(a)

    # then — the cap decision for s3 does not flip because s1 is open this cycle
    assert ("s3", "cap") in [(s.session_id, s.reason) for s in report.session_skipped]
    assert _records(a) == sorted([*FILES["s1"], *FILES["s2"]])


# --------------------------------------------------------------------------- review of #124: mode and the rescan skip


def test_a_session_file_created_on_the_receiving_mac_is_owner_only_along_with_its_new_folders(
    tmp_path, memory_key_store
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, FILES["s1"][1], LINE_2)
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    _run(b)

    # then — a conversation is not a setting: 0600, and the folders made for it are 0700
    target = b.home / FILES["s1"][1]
    assert (target.stat().st_mode & 0o777, target.parent.stat().st_mode & 0o777) == (0o600, 0o700)


def test_an_existing_file_keeps_its_own_mode_when_it_is_extended(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    (b.home / MAIN).chmod(0o640)
    _write(a, MAIN, LINE_1 + LINE_2 + LINE_3)
    _run(a)

    # when
    _run(b)

    # then
    assert (b.home / MAIN).stat().st_mode & 0o777 == 0o640


def test_a_secret_added_to_an_already_published_file_still_refuses_the_whole_session(
    tmp_path, memory_key_store
) -> None:
    # given — the old bytes are published, so they are not scanned again; the new tail is not the old bytes
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _write(a, SUB, LINE_2)
    _run(a)
    _write(a, MAIN, LINE_1 + SECRET_LINE)

    # when
    report = _run(a)

    # then
    assert [(s.session_id, s.reason, s.path) for s in report.session_skipped] == [("s1", "secret", MAIN)]
    assert _records(a) == sorted([MAIN, SUB])


def test_a_published_file_that_has_not_changed_is_not_scanned_again(tmp_path, memory_key_store, monkeypatch) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _run(a)
    scanned: list[str] = []
    original = PatternSecretScanner.scan

    def counting(self, plaintext, *, subject, format):
        scanned.append(subject)
        return original(self, plaintext, subject=subject, format=format)

    monkeypatch.setattr(PatternSecretScanner, "scan", counting)

    # when
    _run(a)

    # then — the repository's own bytes were scanned when they were sealed
    assert MAIN not in scanned
