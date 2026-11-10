# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for Claude Code's session store and open-session probe (issue #56).

Directory shapes and record keys follow Claude Code 2.1.308 (names and keys only); every value is invented.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syncintosh.core.agent_projects import AgentLayout, AgentProject, claude_project_dir_name
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.claude_memory import CLAUDE_PROJECTS
from syncintosh.system import claude_sessions as module
from syncintosh.system.agent_project_index import SystemAgentProjectIndex
from syncintosh.system.claude_sessions import (
    ClaudeSessionActivity,
    ClaudeSessionStore,
    transcript_version,
)
from syncintosh.system.deny_list import SystemDenyList

SESSION = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5b"
OTHER = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5c"
CATALOG = BuiltinCatalog.default()


def _project() -> AgentProject:
    return AgentProject.create(
        name="work", path="code/x", layouts=(AgentLayout.create(preset="claude-sessions", spec=CLAUDE_PROJECTS),)
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "code" / "x").mkdir(parents=True, exist_ok=True)
    return home


def _folder(home: Path) -> Path:
    return home / ".claude" / "projects" / claude_project_dir_name(os.path.realpath(home / "code" / "x"))


def _store(home: Path, version: str | None = "2.1.308", environ: dict | None = None) -> ClaudeSessionStore:
    return ClaudeSessionStore(
        str(home),
        (_project(),),
        SystemAgentProjectIndex(str(home), SystemDenyList(), {} if environ is None else environ),
        SystemDenyList(),
        CATALOG,
        local_version=lambda: version,
    )


def _file(path: Path, data: bytes = b"{}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _files(folder: Path, *relatives: str) -> None:
    """Create each of `relatives` under `folder`."""
    [_file(folder / relative) for relative in relatives]


def _logical(relative: str) -> str:
    return f".claude/projects/@work/{relative}"


# --------------------------------------------------------------------------- listing


def test_a_session_lists_its_transcript_sub_agent_files_and_tool_results(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    folder = _folder(home)
    _files(
        folder,
        f"{SESSION}.jsonl",
        f"{SESSION}/subagents/agent-a1.jsonl",
        f"{SESSION}/subagents/agent-a1.meta.json",
        f"{SESSION}/subagents/agent-a1.forked-skill.json",
        f"{SESSION}/subagents/agent-a1.forked-skill.marker.json",
        f"{SESSION}/tool-results/toolu_01.txt",
    )

    # when
    listing = _store(home).sessions()

    # then
    assert [sorted(f.path for f in unit.files) for unit in listing.units] == [
        sorted(
            _logical(relative)
            for relative in (
                f"{SESSION}.jsonl",
                f"{SESSION}/subagents/agent-a1.jsonl",
                f"{SESSION}/subagents/agent-a1.meta.json",
                f"{SESSION}/subagents/agent-a1.forked-skill.json",
                f"{SESSION}/subagents/agent-a1.forked-skill.marker.json",
                f"{SESSION}/tool-results/toolu_01.txt",
            )
        )
    ]
    assert (listing.units[0].session_id, listing.units[0].project, listing.skipped) == (SESSION, "work", ())


def test_a_listed_file_carries_the_real_path_its_size_and_its_mtime(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    target = _file(_folder(home) / f"{SESSION}.jsonl", b"abcd")
    os.utime(target, (1000, 1000))

    # when
    listed = _store(home).sessions().units[0].files[0]

    # then
    assert (listed.live, listed.size, listed.mtime) == (
        f".claude/projects/{_folder(home).name}/{SESSION}.jsonl",
        4,
        1000.0,
    )


@pytest.mark.parametrize(
    "relative",
    [
        "memory/MEMORY.md",
        "not-a-uuid.jsonl",
        f"{SESSION}.txt",
        f"{SESSION}/notes.md",
        f"{SESSION}/subagents/readme.md",
        f"{SESSION}/other/agent-a1.jsonl",
    ],
)
def test_files_the_preset_does_not_list_are_not_part_of_a_session(tmp_path: Path, relative: str) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")
    _file(_folder(home) / relative)

    # when
    files = _store(home).sessions().units[0].files

    # then
    assert [f.path for f in files] == [_logical(f"{SESSION}.jsonl")]


def test_a_folder_of_a_session_without_a_transcript_lists_nothing(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / SESSION / "tool-results" / "a.txt")

    # when
    listing = _store(home).sessions()

    # then
    assert listing == module.SessionListing()


def test_two_sessions_are_two_units(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")
    _file(_folder(home) / f"{OTHER}.jsonl")

    # when
    ids = [unit.session_id for unit in _store(home).sessions().units]

    # then
    assert ids == [SESSION, OTHER]


def test_a_project_with_no_agent_folder_yet_has_no_sessions(tmp_path: Path) -> None:
    # when
    listing = _store(_home(tmp_path)).sessions()

    # then
    assert listing == module.SessionListing()


@pytest.mark.parametrize(
    "relative",
    [f"{SESSION}.jsonl", f"{SESSION}/subagents/agent-a1.jsonl", f"{SESSION}/tool-results/a.txt"],
)
def test_a_symlink_in_a_session_leaves_the_whole_session_out(tmp_path: Path, relative: str) -> None:
    # given
    home = _home(tmp_path)
    folder = _folder(home)
    _file(folder / f"{SESSION}.jsonl")
    _file(folder / f"{SESSION}/subagents/agent-a1.jsonl")
    _file(folder / f"{SESSION}/tool-results/a.txt")
    (folder / relative).unlink()
    (tmp_path / "target").write_bytes(b"x")
    (folder / relative).symlink_to(tmp_path / "target")

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [(s.session_id, s.reason) for s in listing.skipped]) == ((), [(SESSION, "unknown")])


def test_a_directory_where_a_file_belongs_leaves_the_session_out(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")
    (_folder(home) / SESSION / "subagents" / "nested").mkdir(parents=True)

    # when
    listing = _store(home).sessions()

    # then
    assert [s.reason for s in listing.skipped] == ["unknown"]


def test_a_session_with_more_files_than_the_bound_is_reported_not_listed(tmp_path: Path, monkeypatch) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")
    _files(_folder(home) / SESSION / "tool-results", "0.txt", "1.txt", "2.txt")
    monkeypatch.setattr(module, "MAX_SESSION_FILES", 3)

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [s.reason for s in listing.skipped]) == ((), ["cap"])


def test_a_project_past_the_file_bound_reports_the_sessions_that_do_not_fit(tmp_path: Path, monkeypatch) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")
    _file(_folder(home) / f"{OTHER}.jsonl")
    monkeypatch.setattr(module, "MAX_PROJECT_FILES", 1)

    # when
    listing = _store(home).sessions()

    # then
    assert ([u.session_id for u in listing.units], [(s.session_id, s.reason) for s in listing.skipped]) == (
        [SESSION],
        [(OTHER, "cap")],
    )


def test_an_environment_override_reports_the_project_instead_of_guessing_its_folder(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")

    # when
    listing = _store(home, environ={"CLAUDE_CONFIG_DIR": "/moved"}).sessions()

    # then
    assert (listing.units, [(s.session_id, s.reason) for s in listing.skipped]) == ((), [("work", "unknown")])
    assert "CLAUDE_CONFIG_DIR" in listing.skipped[0].detail


def test_a_project_that_did_not_enable_the_preset_lists_nothing(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _file(_folder(home) / f"{SESSION}.jsonl")
    memory_only = AgentProject.create(
        name="work", path="code/x", layouts=(AgentLayout.create(preset="claude-memory", spec=CLAUDE_PROJECTS),)
    )
    store = ClaudeSessionStore(
        str(home),
        (memory_only,),
        SystemAgentProjectIndex(str(home), SystemDenyList(), {}),
        SystemDenyList(),
        CATALOG,
    )

    # when
    listing = store.sessions()

    # then
    assert listing == module.SessionListing()


# --------------------------------------------------------------------------- which session a path is


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (f"{SESSION}.jsonl", SESSION),
        (f"{SESSION}/subagents/agent-a1.jsonl", SESSION),
        (f"{SESSION}/tool-results/a.txt", SESSION),
        ("memory/MEMORY.md", None),
        ("not-a-uuid.jsonl", None),
        (f"{SESSION}", None),
    ],
)
def test_the_session_of_a_path_is_the_uuid_in_it(relative: str, expected: str | None) -> None:
    # given
    store = _store(Path("/nonexistent-home"))

    # when
    found = store.session_of(_logical(relative))

    # then
    assert found == expected


@pytest.mark.parametrize(
    "path",
    [
        f".claude/projects/@other/{SESSION}.jsonl",
        f".claude/projects/-Users-a-code-x/{SESSION}.jsonl",
        f"elsewhere/@work/{SESSION}.jsonl",
    ],
)
def test_a_path_under_an_unmapped_name_or_a_raw_folder_belongs_to_no_session(path: str) -> None:
    # given
    store = _store(Path("/nonexistent-home"))

    # when
    found = store.session_of(path)

    # then
    assert found is None


# --------------------------------------------------------------------------- the version warning


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'{"type":"summary"}\n{"version":"2.1.300","cwd":"/x"}\n', "2.1.300"),
        (b'{"version":"2.1.300"}\n{"version":"9.9.9"}\n', "2.1.300"),
        (b'not json\n{"version":"2.1.300"}\n', "2.1.300"),
        (b'{"version":"not-a-version"}\n', None),
        (b'{"version":3}\n', None),
        (b"[]\n", None),
        (b"", None),
    ],
)
def test_the_transcripts_version_is_the_first_record_that_has_one(data: bytes, expected: str | None) -> None:
    # when
    version = transcript_version(data)

    # then
    assert version == expected


@pytest.mark.parametrize(
    ("written", "local", "expected"),
    [
        ("2.1.300", "2.1.308", f"session {SESSION} was written by Claude Code 2.1.300; this Mac runs 2.1.308"),
        ("2.1.308", "2.1.308", None),
        ("2.1.300", None, None),
        (None, "2.1.308", None),
    ],
)
def test_a_different_cli_version_is_a_warning_and_an_unknown_one_is_silent(
    written: str | None, local: str | None, expected: str | None
) -> None:
    # given
    data = b"{}\n" if written is None else json.dumps({"version": written}).encode() + b"\n"

    # when
    note = _store(Path("/nonexistent-home"), version=local).note_for(_logical(f"{SESSION}.jsonl"), data, created=True)

    # then
    assert note == expected


def test_only_the_main_transcript_is_checked_for_a_version() -> None:
    # when
    note = _store(Path("/x"), version="9.9.9").note_for(
        _logical(f"{SESSION}/tool-results/a.txt"), b'{"version":"1.0.0"}\n', created=True
    )

    # then
    assert note is None


def test_the_installed_version_is_read_from_the_first_number_of_the_cli_output(monkeypatch) -> None:
    # given
    class Done:
        returncode = 0
        stdout = "2.1.308 (Claude Code)\n"

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: Done())

    # when
    version = module.installed_version()

    # then
    assert version == "2.1.308"


@pytest.mark.parametrize("failure", [OSError("no claude"), module.subprocess.TimeoutExpired("claude", 2)])
def test_an_installed_version_that_cannot_be_read_is_unknown(monkeypatch, failure: Exception) -> None:
    # given
    def fail(*a, **k):
        raise failure

    monkeypatch.setattr(module.subprocess, "run", fail)

    # when
    version = module.installed_version()

    # then
    assert version is None


# --------------------------------------------------------------------------- open sessions


def _activity(home: Path, *, alive: bool = True, started: str | None = "Mon Nov  9 10:00:00 2026"):
    return ClaudeSessionActivity(str(home), alive=lambda pid: alive, started=lambda pid: started)


def _record(home: Path, pid: int, session: str, start: str | None = "Mon Nov  9 10:00:00 2026", **extra) -> None:
    document = {"pid": pid, "sessionId": session, "cwd": "/invented", **extra}
    if start is not None:
        document["procStart"] = start
    _file(home / ".claude" / "sessions" / f"{pid}.json", json.dumps(document).encode())


def test_a_machine_that_has_never_run_the_agent_has_no_open_session(tmp_path: Path) -> None:
    # when
    answer = _activity(tmp_path / "home").is_open(SESSION)

    # then
    assert answer is False


def test_a_live_process_with_the_recorded_start_time_has_the_session_open(tmp_path: Path) -> None:
    # given
    _record(tmp_path, 4242, SESSION)

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is True


def test_a_pid_that_was_recycled_is_not_the_session(tmp_path: Path) -> None:
    # given — the pid is alive, but it started at another time than the file says
    _record(tmp_path, 4242, SESSION)

    # when
    answer = _activity(tmp_path, started="Tue Nov 10 08:00:00 2026").is_open(SESSION)

    # then
    assert answer is False


def test_a_file_left_by_a_crash_for_a_dead_process_is_not_open(tmp_path: Path) -> None:
    # given
    _record(tmp_path, 4242, SESSION)

    # when
    answer = _activity(tmp_path, alive=False).is_open(SESSION)

    # then
    assert answer is False


def test_another_sessions_process_does_not_open_this_one(tmp_path: Path) -> None:
    # given
    _record(tmp_path, 4242, OTHER)

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is False


def test_a_record_with_no_start_time_counts_by_its_live_pid(tmp_path: Path) -> None:
    # given
    _record(tmp_path, 4242, SESSION, start=None)

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is True


def test_a_start_time_that_cannot_be_asked_for_counts_as_open(tmp_path: Path) -> None:
    # given
    _record(tmp_path, 4242, SESSION)

    # when
    answer = _activity(tmp_path, started=None).is_open(SESSION)

    # then
    assert answer is True


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'{"pid": "1", "sessionId": "x"}', b'{"pid": true, "sessionId": "x"}', b"x" * 70_000],
)
def test_a_file_that_cannot_be_read_leaves_the_answer_unknown(tmp_path: Path, content: bytes) -> None:
    # given
    _file(tmp_path / ".claude" / "sessions" / "1.json", content)

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is None


def test_an_open_session_is_found_even_when_another_file_is_unreadable(tmp_path: Path) -> None:
    # given
    _file(tmp_path / ".claude" / "sessions" / "1.json", b"{not json")
    _record(tmp_path, 4242, SESSION)

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is True


def test_a_session_file_that_is_a_symlink_is_not_followed(tmp_path: Path) -> None:
    # given
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    (tmp_path / "real.json").write_text(json.dumps({"pid": 1, "sessionId": SESSION}), encoding="utf-8")
    (tmp_path / ".claude" / "sessions" / "1.json").symlink_to(tmp_path / "real.json")

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is None


def test_more_files_than_the_bound_leaves_the_answer_unknown(tmp_path: Path, monkeypatch) -> None:
    # given
    _record(tmp_path, 1, OTHER)
    _record(tmp_path, 2, OTHER)
    monkeypatch.setattr(module, "MAX_ACTIVITY_FILES", 1)

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is None


def test_a_sessions_path_that_is_a_file_leaves_the_answer_unknown(tmp_path: Path) -> None:
    # given
    _file(tmp_path / ".claude" / "sessions", b"x")

    # when
    answer = _activity(tmp_path).is_open(SESSION)

    # then
    assert answer is None


@pytest.mark.parametrize(("which", "expected"), [("this", True), ("absent", False)])
def test_the_real_liveness_probe_sees_this_process_and_not_an_absent_one(which: str, expected: bool) -> None:
    # when — the pid is looked up inside the test so the test id is the same in every xdist worker
    alive = module.process_alive({"this": os.getpid(), "absent": 2**22 - 3}[which])

    # then
    assert alive is expected


def test_the_real_start_time_probe_answers_for_this_process() -> None:
    # when
    started = module.process_start(os.getpid())

    # then
    assert started is None or started != ""


def test_a_start_time_probe_that_cannot_run_is_unknown(monkeypatch) -> None:
    # given
    def fail(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(module.subprocess, "run", fail)

    # when
    started = module.process_start(1)

    # then
    assert started is None


@pytest.mark.parametrize("linked", [f"{SESSION}/subagents", f"{SESSION}/tool-results", SESSION])
def test_a_linked_session_folder_leaves_the_session_out(tmp_path: Path, linked: str) -> None:
    # given — the folder is a link to a directory that holds files of its own
    home = _home(tmp_path)
    folder = _folder(home)
    _file(folder / f"{SESSION}.jsonl")
    _file(tmp_path / "elsewhere" / "agent-a1.jsonl")
    (folder / linked).parent.mkdir(parents=True, exist_ok=True)
    (folder / linked).symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [s.reason for s in listing.skipped]) == ((), ["unknown"])


def test_the_installed_version_is_asked_for_once_per_store(tmp_path: Path) -> None:
    # given
    asked: list[int] = []
    store = ClaudeSessionStore(
        str(tmp_path),
        (_project(),),
        SystemAgentProjectIndex(str(tmp_path), SystemDenyList(), {}),
        SystemDenyList(),
        CATALOG,
        local_version=lambda: asked.append(1) or "2.1.308",
    )
    data = b'{"version":"2.1.300"}\n'

    # when
    [store.note_for(_logical(f"{SESSION}.jsonl"), data, created=True) for _ in range(3)]

    # then
    assert asked == [1]


def test_an_uppercase_uuid_is_not_the_folder_the_agent_made() -> None:
    # when
    owned = CATALOG.policy_for(_logical(f"{SESSION.upper()}.jsonl"))

    # then
    assert owned is None


def test_claude_version_output_that_is_not_text_is_unknown_not_a_crash(monkeypatch) -> None:
    # given
    def fail(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(module.subprocess, "run", fail)

    # when
    version = module.installed_version()

    # then
    assert version is None
