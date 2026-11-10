# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: Codex sessions across two Macs that keep a project in different places (issue #56).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The shipped
`codex-sessions` preset, the real `CodexSessionStore` and the real flock probe `CodexSessionActivity` are used.
Machine A keeps the project at `src/x` and B at `code/x`. Codex files a rollout by date, so a rollout lands in the
same date tree on both Macs, and only its first record's `cwd` (A's path, never rewritten) names the project. Every
value is invented, with the keys a real `session_meta` has.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.agent_projects import AgentLayout, AgentProject
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.core.sessions import IDLE_SECONDS
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.codex_sessions import CODEX_PROJECTS
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
from syncintosh.system.agent_project_index import SystemAgentProjectIndex
from syncintosh.system.codex_sessions import CodexSessionActivity, CodexSessionStore
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
CATALOG = BuiltinCatalog.default()
T1 = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5b"
T2 = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5c"
ANCESTOR = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5d"
DAY = "2026/11/09"
SECRET = b'{"type":"response_item","payload":{"output":"' + b"AKIA" + b"ABCDEFGHIJKLMNOP" + b'"}}\n'


def _name(thread: str = T1, stamp: str = "2026-11-09T10-00-00") -> str:
    return f"rollout-{stamp}-{thread}.jsonl"


def _rollout(cwd: str, *, thread: str = T1, version: str = "0.157.1", base: str | None = None, more: str = "") -> bytes:
    payload = {
        "id": thread,
        "session_id": thread,
        "timestamp": "2026-11-09T10:00:00.000Z",
        "cwd": cwd,
        "originator": "codex_cli_rs",
        "cli_version": version,
        "creator_user_id": "user-invented",
        "creator_account_id": "account-invented",
        "base_instructions": {"text": "invented instructions"},
    }
    if base is not None:
        payload["history_base"] = {"thread_id": base, "end_ordinal_exclusive": 2, "end_byte_offset": 90}
    first = {"timestamp": "2026-11-09T10:00:00.000Z", "type": "session_meta", "payload": payload}
    return json.dumps(first).encode() + b"\n" + more.encode()


def _event(text: str) -> str:
    return json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": text}}) + "\n"


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
    root: str
    recovery: Path | None = None
    environ: dict[str, str] = field(default_factory=dict)
    version: str | None = "0.157.1"
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))

    @property
    def real_root(self) -> str:
        return os.path.realpath(self.home / self.root)


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _machine(tmp_path: Path, make_store, name: str, remote: str, root: str) -> Machine:
    home = tmp_path / name / "home"
    state = home / ".local" / "share" / "syncintosh"
    (home / root).mkdir(parents=True)
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
        root=root,
    )


def _initialize(machine: Machine, *, import_from: Path | None = None) -> None:
    base = new_machine_config(remote=machine.remote, machine_id=machine.machine_id, preset_names=(), catalog=CATALOG)
    project = AgentProject.create(
        name="work", path=machine.root, layouts=(AgentLayout.create(preset="codex-sessions", spec=CODEX_PROJECTS),)
    )
    save_config(replace(base, agent_projects=(project,)), machine.config_file)
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


def _run(machine: Machine) -> SyncReport:
    active = GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    config = load_config(machine.config_file)
    home = str(machine.home)
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
        config=config,
        home=home,
        branch=active.branch() or BRANCH,
        agent_projects=SystemAgentProjectIndex(home, SystemDenyList(), machine.environ),
        sessions=CodexSessionStore(
            home,
            config.agent_projects,
            SystemAgentProjectIndex(home, SystemDenyList(), machine.environ),
            SystemDenyList(),
            CATALOG,
            local_version=lambda: machine.version,
        ),
        activity=CodexSessionActivity(home),
    )


def _path(machine: Machine, name: str, day: str = DAY) -> Path:
    return machine.home / ".codex" / "sessions" / day / name


def _write(machine: Machine, data: bytes, *, name: str | None = None, day: str = DAY, idle: bool = True) -> Path:
    target = _path(machine, name or _name(), day)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    stamp = time.time() - (IDLE_SECONDS * 4 if idle else 5)
    os.utime(target, (stamp, stamp))
    return target


def _hold(machine: Machine, thread: str) -> int:
    """Hold the flock a Codex writer holds on this thread, so the thread is open."""
    lock = machine.home / ".codex" / "thread-writer-locks" / f"{thread}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


def _records(machine: Machine) -> list[str]:
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return sorted(record.path for record in manifest.records.values())


def _logical(name: str, day: str = DAY) -> str:
    return f".codex/sessions/@work/{day}/{name}"


def _pair(tmp_path: Path, make_store):
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote, "src/x")
    _initialize(a)
    b = _machine(tmp_path, make_store, "machine-b", remote, "code/x")
    return a, b


def _synced_pair(tmp_path: Path, make_store):
    """A has published one idle rollout; B has it byte for byte."""
    a, b = _pair(tmp_path, make_store)
    _write(a, _rollout(a.real_root, more=_event("hello")))
    _run(a)
    _initialize(b, import_from=a.recovery)
    _run(b)
    return a, b


def test_a_rollout_collected_on_one_mac_lands_in_the_same_date_tree_on_the_other(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    data = _rollout(a.real_root, more=_event("hello"))
    _write(a, data)
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then — byte for byte, with no per-project folder anywhere
    assert _path(b, _name()).read_bytes() == data
    assert sorted(p.name for p in (b.home / ".codex").iterdir()) == ["sessions"]
    assert (report.session_skipped, report.has_conflicts) == ((), False)


def test_the_cwd_and_the_account_identifiers_are_kept_as_they_were(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    _run(b)

    # then — D4 and D7: nothing in the first record is rewritten or dropped
    first = json.loads(_path(b, _name()).read_bytes().splitlines()[0])["payload"]
    assert (first["cwd"], first["creator_user_id"], first["creator_account_id"]) == (
        a.real_root,
        "user-invented",
        "account-invented",
    )


def test_the_record_names_the_project_not_the_machines_path_or_the_date_tree(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))

    # when
    _run(a)

    # then
    assert _records(a) == [_logical(_name())]
    assert [p.name for p in (a.repo / "objects").iterdir() if "work" in p.name or "2026" in p.name] == []


def test_a_new_rollout_is_reported_with_the_command_that_lets_the_picker_see_it(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then
    assert [n for n in report.notes if f"codex resume {T1}" in n] != []


def test_a_rollout_of_another_project_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, _rollout("/Users/someone/elsewhere"))

    # when
    report = _run(a)

    # then
    assert (_records(a), report.session_skipped) == ([], ())


def test_a_rollout_a_moment_old_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root), idle=False)

    # when
    report = _run(a)

    # then
    assert (_records(a), [(s.session_id, s.reason) for s in report.session_skipped]) == ([], [(T1, "open")])


def test_a_thread_whose_writer_holds_the_lock_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    held = _hold(a, T1)

    # when
    report = _run(a)
    os.close(held)

    # then
    assert (_records(a), [s.reason for s in report.session_skipped]) == ([], ["open"])


def test_a_lock_file_nobody_holds_does_not_hold_a_thread_back(tmp_path, memory_key_store) -> None:
    # given — the file survives a crash, and presence proves nothing
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    os.close(_hold(a, T1))

    # when
    _run(a)

    # then
    assert _records(a) == [_logical(_name())]


def test_a_longer_copy_extends_the_local_rollout_with_a_fresh_mtime_and_no_resume_hint(
    tmp_path, memory_key_store
) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    old = time.time() - IDLE_SECONDS * 4
    os.utime(_path(b, _name()), (old, old))
    longer = _rollout(a.real_root, more=_event("hello") + _event("more"))
    _write(a, longer)
    _run(a)
    before = time.time()

    # when
    report = _run(b)

    # then
    assert _path(b, _name()).read_bytes() == longer
    assert _path(b, _name()).stat().st_mtime >= before - 1 > old
    assert (report.restore_points, [n for n in report.notes if "codex resume" in n]) == ((), [])


def test_a_continuation_started_on_the_other_mac_travels_back_although_its_cwd_is_not_local(
    tmp_path, memory_key_store
) -> None:
    # given — B resumed A's rollout in `code/x`; Codex appends and the first record still names A's path
    a, b = _synced_pair(tmp_path, memory_key_store)
    longer = _rollout(a.real_root, more=_event("hello") + _event("on b"))
    _write(b, longer)

    # when
    _run(b)
    _run(a)

    # then
    assert _path(a, _name()).read_bytes() == longer


def test_a_rollout_continued_on_both_macs_keeps_the_repos_copy_and_is_reported(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root, more=_event("hello") + _event("from a")))
    _run(a)
    mine = _rollout(a.real_root, more=_event("hello") + _event("from b"))
    _write(b, mine)

    # when
    report = _run(b)

    # then
    assert (report.has_conflicts, _path(b, _name()).read_bytes()) == (False, mine)
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [(T1, "forked")]


def test_a_rollout_the_agent_compressed_on_the_receiving_mac_is_not_written_beside_it(
    tmp_path, memory_key_store
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    _run(a)
    _initialize(b, import_from=a.recovery)
    compressed = _path(b, _name() + ".zst")
    compressed.parent.mkdir(parents=True)
    compressed.write_bytes(b"zstd")

    # when
    report = _run(b)

    # then
    assert (_path(b, _name()).exists(), [(s.session_id, s.reason) for s in report.session_skipped]) == (
        False,
        [(T1, "archived")],
    )


def test_a_rollout_the_agent_archived_on_the_receiving_mac_is_not_brought_back(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    _run(a)
    _initialize(b, import_from=a.recovery)
    archived = b.home / ".codex" / "archived_sessions" / _name()
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"x")

    # when
    _run(b)

    # then
    assert _path(b, _name()).exists() is False


def test_archived_sessions_are_never_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    archived = a.home / ".codex" / "archived_sessions" / _name()
    archived.parent.mkdir(parents=True)
    archived.write_bytes(_rollout(a.real_root))

    # when
    _run(a)

    # then
    assert _records(a) == []


def test_a_rollout_whose_history_base_is_missing_here_says_so(tmp_path, memory_key_store) -> None:
    # given — the child builds on a parent rollout that belongs to another project, so it is not synced
    a, b = _pair(tmp_path, memory_key_store)
    child = _rollout(a.real_root, thread=T2, base=ANCESTOR)
    _write(a, child, name=_name(T2, "2026-11-09T11-00-00"))
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then
    assert [n for n in report.notes if f"builds on rollout {ANCESTOR}" in n] != []


def test_a_history_base_written_in_the_same_cycle_is_not_reported_missing(tmp_path, memory_key_store) -> None:
    # given — parent and child are both this project's, so both are written, whichever comes first
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root, thread=ANCESTOR), name=_name(ANCESTOR))
    _write(a, _rollout(a.real_root, thread=T2, base=ANCESTOR), name=_name(T2, "2026-11-09T11-00-00"))
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then
    assert [n for n in report.notes if "builds on rollout" in n] == []


def test_a_secret_in_a_rollout_leaves_that_session_out_and_the_rest_goes_on(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root, more=SECRET.decode()))
    _write(a, _rollout(a.real_root, thread=T2), name=_name(T2, "2026-11-09T11-00-00"))

    # when
    report = _run(a)

    # then
    assert _records(a) == [_logical(_name(T2, "2026-11-09T11-00-00"))]
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [(T1, "secret")]
    assert "ABCDEFGHIJKLMNOP" not in repr(report)


def test_a_transcript_from_another_cli_version_is_written_with_a_warning(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root, version="0.150.0"))
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then
    assert [n for n in report.notes if "written by Codex 0.150.0; this Mac runs 0.157.1" in n] != []


def test_an_environment_override_collects_and_writes_nothing(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    _run(a)
    _initialize(b, import_from=a.recovery)
    b.environ["CODEX_HOME"] = str(b.home / "elsewhere")

    # when
    report = _run(b)

    # then
    assert [d for d in report.deferred if "CODEX_HOME" in d.reason] != []
    assert (b.home / ".codex").exists() is False


def test_a_project_not_cloned_on_the_receiving_mac_defers_and_creates_nothing(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, _rollout(a.real_root))
    _run(a)
    _initialize(b, import_from=a.recovery)
    (b.home / b.root).rmdir()

    # when
    report = _run(b)

    # then
    assert ((b.home / ".codex").exists(), [d.path for d in report.deferred]) == (False, [_logical(_name())])


def test_a_rollout_removed_here_is_not_recreated(tmp_path, memory_key_store) -> None:
    # given
    _, b = _synced_pair(tmp_path, memory_key_store)
    _path(b, _name()).unlink()

    # when
    _run(b)

    # then
    assert _path(b, _name()).exists() is False
