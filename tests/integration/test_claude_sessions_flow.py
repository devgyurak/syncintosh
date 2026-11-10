# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: Claude Code sessions across two Macs that keep a project in different places (issue #56).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The shipped
`claude-sessions` preset, the real `ClaudeSessionStore` and the real `ClaudeSessionActivity` are used; only the process
probes are fakes. Machine A keeps the project at `src/x` and B at `code/x`, so the folder Claude Code names for it
differs.
Every record's content is invented, with the keys a real record has (`type`, `sessionId`, `cwd`, `version`).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from _harness import run_sync

from syncintosh.core.agent_projects import AgentLayout, AgentProject, claude_project_dir_name
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.core.sessions import IDLE_SECONDS
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.claude_memory import CLAUDE_PROJECTS
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport
from syncintosh.system.agent_project_index import SystemAgentProjectIndex
from syncintosh.system.claude_sessions import ClaudeSessionActivity, ClaudeSessionStore
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.repo_files import FileRepo
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
CATALOG = BuiltinCatalog.default()
S1 = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5b"
S2 = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5c"
MAIN = f"{S1}.jsonl"
SUB = f"{S1}/subagents/agent-a1.jsonl"
META = f"{S1}/subagents/agent-a1.meta.json"
RESULT = f"{S1}/tool-results/toolu_01.txt"


def _record(text: str, *, cwd: str = "/Users/someone/src/x", version: str = "2.1.308") -> bytes:
    document = {"type": "user", "sessionId": S1, "cwd": cwd, "version": version, "gitBranch": "main", "text": text}
    return json.dumps(document).encode() + b"\n"


LINE_1, LINE_2, LINE_3 = _record("hello"), _record("hi"), _record("more")
SECRET = b'{"type":"tool","out":"' + b"AKIA" + b"ABCDEFGHIJKLMNOP" + b'"}\n'


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
    alive: set[int] = field(default_factory=set)
    version: str | None = "2.1.308"
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))


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
        name="work",
        path=machine.root,
        layouts=(AgentLayout.create(preset="claude-sessions", spec=CLAUDE_PROJECTS),),
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
    GitCLITransport(machine.repo)
    EnvelopeCipher(machine.store)
    config = load_config(machine.config_file)
    home = str(machine.home)
    return run_sync(
        Path(home),
        machine.store,
        codec=machine.codec,
        config=config,
        adapters={},
        catalog=CATALOG,
        apps=NO_APP_INFO,
        agent_projects=SystemAgentProjectIndex(home, SystemDenyList(), {}),
        sessions=ClaudeSessionStore(
            home,
            config.agent_projects,
            SystemAgentProjectIndex(home, SystemDenyList(), {}),
            SystemDenyList(),
            CATALOG,
            local_version=lambda: machine.version,
        ),
        activity=ClaudeSessionActivity(
            home, alive=lambda pid: pid in machine.alive, started=lambda pid: "Mon Nov  9 10:00:00 2026"
        ),
    )


def _folder(machine: Machine) -> Path:
    """Where Claude Code keeps this machine's project: named after the real root, so it differs per machine."""
    return (
        machine.home / ".claude" / "projects" / claude_project_dir_name(os.path.realpath(machine.home / machine.root))
    )


def _write(machine: Machine, relative: str, data: bytes, *, idle: bool = True) -> Path:
    target = _folder(machine) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    stamp = time.time() - (IDLE_SECONDS * 4 if idle else 5)
    os.utime(target, (stamp, stamp))
    return target


def _open_in_agent(machine: Machine, session: str, pid: int = 4242) -> None:
    """Make the agent's own signal say `session` is open on this machine."""
    signal = machine.home / ".claude" / "sessions" / f"{pid}.json"
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.write_text(
        json.dumps({"pid": pid, "sessionId": session, "procStart": "Mon Nov  9 10:00:00 2026", "cwd": "/x"}),
        encoding="utf-8",
    )
    machine.alive.add(pid)


def _records(machine: Machine) -> list[str]:
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return sorted(record.path for record in manifest.records.values())


def _logical(relative: str) -> str:
    return f".claude/projects/@work/{relative}"


def _pair(tmp_path: Path, make_store):
    """A keeps the project at `src/x`, B at `code/x`; B has the ring and an empty folder for it."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote, "src/x")
    _initialize(a)
    b = _machine(tmp_path, make_store, "machine-b", remote, "code/x")
    return a, b


def _synced_pair(tmp_path: Path, make_store):
    """A has published one idle session, and B has it byte for byte."""
    a, b = _pair(tmp_path, make_store)
    _write(a, MAIN, LINE_1 + LINE_2)
    _run(a)
    _initialize(b, import_from=a.recovery)
    _run(b)
    return a, b


def test_a_session_collected_on_one_mac_resumes_from_the_folder_the_other_derives(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2)
    _write(a, SUB, LINE_3)
    _write(a, META, b'{"agentType":"general"}\n')
    _write(a, RESULT, b"a large tool output\n")
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then — every file landed under B's own folder for the project, byte for byte
    assert _folder(a).name != _folder(b).name
    assert [(_folder(b) / p).read_bytes() for p in (MAIN, SUB, META, RESULT)] == [
        LINE_1 + LINE_2,
        LINE_3,
        b'{"agentType":"general"}\n',
        b"a large tool output\n",
    ]
    assert (report.session_skipped, report.has_conflicts) == ((), False)


def test_the_records_name_the_project_not_either_machines_folder(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _write(a, RESULT, b"out\n")

    # when
    _run(a)

    # then
    assert _records(a) == sorted([_logical(MAIN), _logical(RESULT)])
    assert [p.name for p in (a.repo / "objects").iterdir() if "work" in p.name or S1 in p.name] == []


def test_a_transcripts_cwd_is_never_rewritten(tmp_path, memory_key_store) -> None:
    # given — A's records say `/Users/someone/src/x`, which is not where B keeps the project
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    _run(b)

    # then
    assert b"/Users/someone/src/x" in (_folder(b) / MAIN).read_bytes()


def test_a_session_written_a_moment_ago_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1, idle=False)

    # when
    report = _run(a)

    # then
    assert (_records(a), [(s.session_id, s.reason) for s in report.session_skipped]) == ([], [(S1, "open")])


def test_an_old_session_the_agents_own_signal_says_is_open_is_not_collected(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _open_in_agent(a, S1)

    # when
    report = _run(a)

    # then
    assert (_records(a), [s.reason for s in report.session_skipped]) == ([], ["open"])


def test_a_leftover_signal_of_a_dead_process_does_not_hold_a_session_back(tmp_path, memory_key_store) -> None:
    # given — the file is there but no process owns it (a crash)
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _open_in_agent(a, S1)
    a.alive.clear()

    # when
    _run(a)

    # then
    assert _records(a) == [_logical(MAIN)]


def test_a_longer_copy_extends_the_local_one_with_a_fresh_mtime(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    old = time.time() - IDLE_SECONDS * 4
    os.utime(_folder(b) / MAIN, (old, old))
    _write(a, MAIN, LINE_1 + LINE_2 + LINE_3)
    _run(a)
    before = time.time()

    # when
    report = _run(b)

    # then — the cleanup that reaps by mtime would not take it
    assert (_folder(b) / MAIN).read_bytes() == LINE_1 + LINE_2 + LINE_3
    assert report.restore_points == ()
    assert (_folder(b) / MAIN).stat().st_mtime >= before - 1 > old


def test_a_session_open_on_the_receiving_mac_is_not_written_over(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2 + LINE_3)
    _run(a)
    _open_in_agent(b, S1)

    # when
    report = _run(b)

    # then
    assert ((_folder(b) / MAIN).read_bytes(), [s.reason for s in report.session_skipped]) == (
        LINE_1 + LINE_2,
        ["open"],
    )


def test_a_session_continued_on_both_macs_keeps_the_repos_copy_and_is_reported(tmp_path, memory_key_store) -> None:
    # given
    a, b = _synced_pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1 + LINE_2 + _record("from a"))
    _run(a)
    _write(b, MAIN, LINE_1 + LINE_2 + _record("from b"))

    # when
    report = _run(b)

    # then
    assert report.has_conflicts is False
    assert (_folder(b) / MAIN).read_bytes() == LINE_1 + LINE_2 + _record("from b")
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [(S1, "forked")]


def test_a_secret_in_a_tool_result_leaves_the_whole_session_out_and_other_sessions_go(
    tmp_path, memory_key_store
) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _write(a, RESULT, SECRET)
    _write(a, f"{S2}.jsonl", LINE_2)

    # when
    report = _run(a)

    # then
    assert _records(a) == [_logical(f"{S2}.jsonl")]
    assert [(s.session_id, s.reason, s.path) for s in report.session_skipped] == [(S1, "secret", _logical(RESULT))]
    assert "ABCDEFGHIJKLMNOP" not in repr(report)


def test_a_file_over_the_cap_leaves_its_session_out(tmp_path, memory_key_store, monkeypatch) -> None:
    # given — the cap is lowered so the test does not write 48 MiB
    a, _ = _pair(tmp_path, memory_key_store)
    monkeypatch.setattr("syncintosh.core.sessions.MAX_FILE_BYTES", 16)
    _write(a, MAIN, b"x" * 17)
    _write(a, f"{S2}.jsonl", b"y" * 16)

    # when
    report = _run(a)

    # then
    assert _records(a) == [_logical(f"{S2}.jsonl")]
    assert [(s.session_id, s.reason) for s in report.session_skipped] == [(S1, "cap")]


def test_a_transcript_from_another_cli_version_is_written_with_a_warning(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, _record("hello", version="2.1.300"))
    _run(a)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then
    assert (_folder(b) / MAIN).exists()
    assert f"session {S1} was written by Claude Code 2.1.300; this Mac runs 2.1.308" in report.notes


def test_an_unknown_local_version_writes_without_a_warning(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, _record("hello", version="2.1.300"))
    _run(a)
    _initialize(b, import_from=a.recovery)
    b.version = None

    # when
    report = _run(b)

    # then
    assert [note for note in report.notes if "Claude Code" in note] == []


def test_a_project_not_cloned_on_the_receiving_mac_defers_and_creates_nothing(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, MAIN, LINE_1)
    _run(a)
    _initialize(b, import_from=a.recovery)
    (b.home / b.root).rmdir()

    # when
    report = _run(b)

    # then
    assert ((b.home / ".claude").exists(), [d.path for d in report.deferred]) == (False, [_logical(MAIN)])


def test_a_session_removed_here_is_not_recreated(tmp_path, memory_key_store) -> None:
    # given
    _, b = _synced_pair(tmp_path, memory_key_store)
    (_folder(b) / MAIN).unlink()

    # when
    _run(b)

    # then
    assert (_folder(b) / MAIN).exists() is False
