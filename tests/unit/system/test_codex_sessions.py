# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for Codex's session store and writer-lock probe (issue #56).

File names and record keys follow codex-cli 0.157.1 (`rollout_file_name.rs`, `SessionMeta`); every value is invented.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from syncintosh.core.agent_projects import AgentLayout, AgentProject
from syncintosh.core.sessions import SessionListing
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.codex_sessions import CODEX_PROJECTS
from syncintosh.system import codex_sessions as module
from syncintosh.system.agent_project_index import SystemAgentProjectIndex
from syncintosh.system.agent_sessions import CombinedSessionActivity, CombinedSessionStore
from syncintosh.system.codex_sessions import CodexSessionActivity, CodexSessionStore, read_meta
from syncintosh.system.deny_list import SystemDenyList

THREAD = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5b"
OTHER = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5c"
ROLLOUT_2 = "0197a1b2-3c4d-5e6f-8a9b-0c1d2e3f4a5d"
CATALOG = BuiltinCatalog.default()


def _project() -> AgentProject:
    return AgentProject.create(
        name="work", path="code/x", layouts=(AgentLayout.create(preset="codex-sessions", spec=CODEX_PROJECTS),)
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "code" / "x").mkdir(parents=True, exist_ok=True)
    return home


def _root(home: Path) -> str:
    return os.path.realpath(home / "code" / "x")


def _store(home: Path, version: str | None = "0.157.1", environ: dict | None = None) -> CodexSessionStore:
    return CodexSessionStore(
        str(home),
        (_project(),),
        SystemAgentProjectIndex(str(home), SystemDenyList(), {} if environ is None else environ),
        SystemDenyList(),
        CATALOG,
        local_version=lambda: version,
    )


def _meta(cwd: str, *, thread: str = THREAD, version: str = "0.157.1", base: str | None = None) -> bytes:
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
        payload["history_base"] = {"thread_id": base, "end_ordinal_exclusive": 3, "end_byte_offset": 120}
    line = {"timestamp": "2026-11-09T10:00:00.000Z", "type": "session_meta", "payload": payload}
    return json.dumps(line).encode() + b"\n" + b'{"type":"event_msg","payload":{"type":"user_message"}}\n'


def _name(thread: str = THREAD, rollout: str | None = None, stamp: str = "2026-11-09T10-00-00") -> str:
    return f"rollout-{stamp}-{thread}" + ("" if rollout is None else f"_{rollout}") + ".jsonl"


def _rollout(home: Path, data: bytes, *, day: str = "2026/11/09", name: str | None = None) -> Path:
    target = home / ".codex" / "sessions" / day / (name or _name())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _logical(relative: str) -> str:
    return f".codex/sessions/@work/{relative}"


# --------------------------------------------------------------------------- the first record


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (_meta("/a/b"), ("/a/b", "0.157.1", None)),
        (_meta("/a/b", base=OTHER), ("/a/b", "0.157.1", OTHER)),
        (b"\n  \nnot json\n" + _meta("/a/b"), ("/a/b", "0.157.1", None)),
        (_meta("relative/path"), (None, "0.157.1", None)),
        (_meta("/a/b", version="x"), ("/a/b", None, None)),
        (b'{"type":"response_item","payload":{}}\n' + _meta("/a/b"), None),
        (b'{"type":"turn_context","payload":{}}\n' + _meta("/a/b"), ("/a/b", "0.157.1", None)),
        (b"[]\n", None),
        (b"", None),
        (b'{"type":"session_meta","payload":"x"}\n', None),
    ],
)
def test_the_session_meta_gives_only_the_cwd_the_cli_version_and_the_history_base(head: bytes, expected) -> None:
    # when
    meta = read_meta(head)

    # then
    assert (None if meta is None else (meta.cwd, meta.cli_version, meta.history_base)) == expected


def test_a_session_meta_after_too_many_records_is_not_looked_for() -> None:
    # given
    head = b'{"type":"x"}\n' * 9 + _meta("/a/b")

    # when
    meta = read_meta(head)

    # then
    assert meta is None


# --------------------------------------------------------------------------- listing


def test_a_rollout_whose_cwd_is_the_projects_root_is_a_session_of_that_project(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    target = _rollout(home, _meta(_root(home)))
    os.utime(target, (1000, 1000))

    # when
    listing = _store(home).sessions()

    # then
    unit = listing.units[0]
    assert (unit.session_id, unit.project, listing.skipped) == (THREAD, "work", ())
    assert [(f.path, f.live, f.size, f.mtime) for f in unit.files] == [
        (
            _logical(f"2026/11/09/{_name()}"),
            f".codex/sessions/2026/11/09/{_name()}",
            target.stat().st_size,
            1000.0,
        )
    ]


def test_a_rollout_of_another_project_is_not_listed(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta("/somewhere/else"))

    # when
    listing = _store(home).sessions()

    # then
    assert listing.units == ()


def test_a_cwd_that_reaches_the_root_through_a_link_is_the_same_project(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    (tmp_path / "alias").symlink_to(home / "code" / "x")
    _rollout(home, _meta(str(tmp_path / "alias")))

    # when
    listing = _store(home).sessions()

    # then
    assert [u.session_id for u in listing.units] == [THREAD]


def test_rollouts_on_different_days_are_each_listed(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)), day="2026/11/08", name=_name(stamp="2026-11-08T09-00-00"))
    _rollout(home, _meta(_root(home), thread=OTHER), day="2026/11/09", name=_name(OTHER))

    # when
    ids = sorted(u.session_id for u in _store(home).sessions().units)

    # then
    assert ids == [THREAD, OTHER]


def test_a_reverted_thread_has_one_session_with_both_rollouts(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)))
    _rollout(home, _meta(_root(home), base=THREAD), name=_name(THREAD, ROLLOUT_2, "2026-11-09T11-00-00"))

    # when
    units = _store(home).sessions().units

    # then
    assert [(u.session_id, len(u.files)) for u in units] == [(THREAD, 2)]


@pytest.mark.parametrize(
    ("day", "name"),
    [
        ("2026/11/09", _name() + ".zst"),
        ("2026/11/09", "rollout-2026-11-09T10-00-00-not-a-uuid.jsonl"),
        ("2026/11/09", "notes.txt"),
        ("2026/1/09", _name()),
        ("26/11/09", _name()),
        ("2026/11", _name()),
    ],
)
def test_only_canonical_rollouts_in_canonical_date_folders_are_listed(tmp_path: Path, day: str, name: str) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)), day=day, name=name)

    # when
    listing = _store(home).sessions()

    # then
    assert listing.units == ()


def test_archived_sessions_are_never_listed(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    archived = home / ".codex" / "archived_sessions" / _name()
    archived.parent.mkdir(parents=True)
    archived.write_bytes(_meta(_root(home)))

    # when
    listing = _store(home).sessions()

    # then
    assert listing == SessionListing()


def test_a_symlinked_rollout_is_not_listed_and_is_counted_as_unmatched(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    real = tmp_path / "real.jsonl"
    real.write_bytes(_meta(_root(home)))
    link = home / ".codex" / "sessions" / "2026" / "11" / "09" / _name()
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [s.reason for s in listing.skipped]) == ((), ["unknown"])


def test_a_linked_date_folder_is_not_followed(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "09"
    elsewhere.mkdir(parents=True)
    (elsewhere / _name()).write_bytes(_meta(_root(home)))
    (home / ".codex" / "sessions" / "2026" / "11").mkdir(parents=True)
    (home / ".codex" / "sessions" / "2026" / "11" / "09").symlink_to(elsewhere, target_is_directory=True)

    # when
    listing = _store(home).sessions()

    # then
    assert listing.units == ()


def test_rollouts_that_cannot_be_matched_are_counted_in_one_report(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, b"not a record\n")
    _rollout(home, b'{"type":"response_item","payload":{}}\n', name=_name(OTHER))

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [(s.session_id, s.reason) for s in listing.skipped]) == ((), [("codex", "unknown")])
    assert "2 rollout" in listing.skipped[0].detail


def test_a_head_larger_than_the_bound_is_not_matched(tmp_path: Path, monkeypatch) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)))
    monkeypatch.setattr(module, "MAX_META_BYTES", 10)

    # when
    listing = _store(home).sessions()

    # then
    assert listing.units == ()


def test_a_tree_past_the_rollout_bound_is_reported(tmp_path: Path, monkeypatch) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)))
    _rollout(home, _meta(_root(home), thread=OTHER), name=_name(OTHER))
    monkeypatch.setattr(module, "MAX_ROLLOUTS", 1)

    # when
    listing = _store(home).sessions()

    # then
    assert ([u.session_id for u in listing.units], [s.reason for s in listing.skipped]) == ([THREAD], ["unknown"])


def test_a_thread_with_more_rollouts_than_the_bound_is_reported(tmp_path: Path, monkeypatch) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)))
    _rollout(home, _meta(_root(home)), name=_name(THREAD, ROLLOUT_2, "2026-11-09T11-00-00"))
    monkeypatch.setattr(module, "MAX_SESSION_FILES", 1)

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [(s.session_id, s.reason) for s in listing.skipped]) == ((), [(THREAD, "cap")])


def test_an_environment_override_reports_the_project_instead_of_guessing(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)))

    # when
    listing = _store(home, environ={"CODEX_HOME": "/moved"}).sessions()

    # then
    assert (listing.units, [(s.session_id, s.reason) for s in listing.skipped]) == ((), [("work", "unknown")])
    assert "CODEX_HOME" in listing.skipped[0].detail


def test_a_project_that_did_not_enable_the_preset_lists_nothing(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)))
    other = AgentProject.create(
        name="work", path="code/x", layouts=(AgentLayout.create(preset="claude-memory", spec=CODEX_PROJECTS),)
    )
    store = CodexSessionStore(
        str(home),
        (other,),
        SystemAgentProjectIndex(str(home), SystemDenyList(), {}),
        SystemDenyList(),
        CATALOG,
    )

    # when
    listing = store.sessions()

    # then
    assert listing == SessionListing()


def test_a_home_with_no_codex_tree_lists_nothing(tmp_path: Path) -> None:
    # when
    listing = _store(_home(tmp_path)).sessions()

    # then
    assert listing == SessionListing()


# --------------------------------------------------------------------------- which session a path is


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (f"2026/11/09/{_name()}", THREAD),
        (f"2026/11/09/{_name(THREAD, ROLLOUT_2)}", THREAD),
        (f"2026/11/09/{_name()}.zst", None),
        ("2026/11/09/state.sqlite", None),
        (f"2026/1/09/{_name()}", None),
        (_name(), None),
    ],
)
def test_the_session_of_a_path_is_the_thread_id_in_the_file_name(relative: str, expected: str | None) -> None:
    # given
    store = _store(Path("/nonexistent-home"))

    # when
    found = store.session_of(_logical(relative))

    # then
    assert found == expected


@pytest.mark.parametrize(
    "path",
    [f".codex/sessions/@other/2026/11/09/{_name()}", f".codex/sessions/2026/11/09/{_name()}"],
)
def test_a_path_under_an_unmapped_name_or_a_raw_date_tree_belongs_to_no_session(path: str) -> None:
    # when
    found = _store(Path("/nonexistent-home")).session_of(path)

    # then
    assert found is None


# --------------------------------------------------------------------------- retired


def test_a_rollout_the_agent_compressed_here_is_retired(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    compressed = home / ".codex" / "sessions" / "2026" / "11" / "09" / (_name() + ".zst")
    compressed.parent.mkdir(parents=True)
    compressed.write_bytes(b"zstd")

    # when
    retired = _store(home).retired(_logical(f"2026/11/09/{_name()}"))

    # then
    assert retired is True


def test_a_rollout_the_agent_archived_here_is_retired(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    archived = home / ".codex" / "archived_sessions" / _name()
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"x")

    # when
    retired = _store(home).retired(_logical(f"2026/11/09/{_name()}"))

    # then
    assert retired is True


@pytest.mark.parametrize("relative", [f"2026/11/09/{_name()}", "2026/11/09/state.sqlite"])
def test_a_rollout_nobody_retired_is_not(tmp_path: Path, relative: str) -> None:
    # when
    retired = _store(_home(tmp_path)).retired(_logical(relative))

    # then
    assert retired is False


# --------------------------------------------------------------------------- what a person is told


def test_a_new_rollout_comes_with_the_command_that_lets_the_picker_see_it(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)

    # when
    note = _store(home).note_for(_logical(f"2026/11/09/{_name()}"), _meta("/a"), created=True)

    # then
    assert note == f"session {THREAD}: it stays out of the Codex picker until it is resumed once: codex resume {THREAD}"


def test_an_extension_of_a_known_rollout_needs_no_resume_hint(tmp_path: Path) -> None:
    # when
    note = _store(_home(tmp_path)).note_for(_logical(f"2026/11/09/{_name()}"), _meta("/a"), created=False)

    # then
    assert note is None


@pytest.mark.parametrize(
    ("written", "local", "expected"),
    [("0.150.0", "0.157.1", True), ("0.157.1", "0.157.1", False), ("0.150.0", None, False)],
)
def test_a_different_cli_version_is_a_warning_and_an_unknown_one_is_silent(
    tmp_path: Path, written: str, local: str | None, expected: bool
) -> None:
    # when
    note = _store(_home(tmp_path), version=local).note_for(
        _logical(f"2026/11/09/{_name()}"), _meta("/a", version=written), created=False
    )

    # then
    assert (note is not None and f"written by Codex {written}; this Mac runs {local}" in note) is expected


def test_a_rollout_whose_history_base_is_not_here_says_so(tmp_path: Path) -> None:
    # when
    note = _store(_home(tmp_path)).note_for(_logical(f"2026/11/09/{_name()}"), _meta("/a", base=OTHER), created=False)

    # then
    assert note == f"session {THREAD}: it builds on rollout {OTHER}, which is not on this Mac"


@pytest.mark.parametrize("where", ["sessions", "archived", "compressed"])
def test_a_history_base_that_is_here_is_not_reported(tmp_path: Path, where: str) -> None:
    # given
    home = _home(tmp_path)
    ancestor = _name(OTHER)
    {
        "sessions": lambda: _rollout(home, b"x", name=ancestor),
        "archived": lambda: _archive(home, ancestor),
        "compressed": lambda: _rollout(home, b"x", name=ancestor + ".zst"),
    }[where]()

    # when
    note = _store(home).note_for(_logical(f"2026/11/09/{_name()}"), _meta("/a", base=OTHER), created=False)

    # then
    assert note is None


def _archive(home: Path, name: str) -> None:
    target = home / ".codex" / "archived_sessions" / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")


def test_a_path_no_session_owns_has_no_note(tmp_path: Path) -> None:
    # when
    note = _store(_home(tmp_path)).note_for(".codex/sessions/@other/x", b"{}", created=True)

    # then
    assert note is None


def test_the_installed_version_is_asked_for_once_per_store(tmp_path: Path) -> None:
    # given
    asked: list[int] = []
    store = CodexSessionStore(
        str(tmp_path),
        (_project(),),
        SystemAgentProjectIndex(str(tmp_path), SystemDenyList(), {}),
        SystemDenyList(),
        CATALOG,
        local_version=lambda: asked.append(1) or "0.157.1",
    )

    # when
    [store.note_for(_logical(f"2026/11/09/{_name()}"), _meta("/a", version="0.1.0"), created=False) for _ in range(3)]

    # then
    assert asked == [1]


def test_the_installed_version_is_read_from_the_cli_output(monkeypatch) -> None:
    # given
    class Done:
        returncode = 0
        stdout = "codex-cli 0.157.1\n"

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: Done())

    # when
    version = module.installed_version()

    # then
    assert version == "0.157.1"


@pytest.mark.parametrize("failure", [OSError("none"), module.subprocess.TimeoutExpired("codex", 2)])
def test_an_installed_version_that_cannot_be_read_is_unknown(monkeypatch, failure: Exception) -> None:
    # given
    def fail(*a, **k):
        raise failure

    monkeypatch.setattr(module.subprocess, "run", fail)

    # when
    version = module.installed_version()

    # then
    assert version is None


# --------------------------------------------------------------------------- the writer lock


def _locks(home: Path) -> Path:
    directory = home / ".codex" / "thread-writer-locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _hold(path: Path) -> int:
    """Hold an exclusive flock on `path` the way a Codex writer does; the caller closes the descriptor."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


def test_a_machine_with_no_lock_folder_has_no_open_thread(tmp_path: Path) -> None:
    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert answer is False


def test_a_thread_with_no_lock_file_is_not_open(tmp_path: Path) -> None:
    # given
    _locks(tmp_path)

    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert answer is False


def test_a_lock_file_left_by_a_crash_that_nobody_holds_is_not_open(tmp_path: Path) -> None:
    # given — the file is there, but no writer holds the flock
    (_locks(tmp_path) / f"{THREAD}.lock").write_bytes(b"")

    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert answer is False


def test_a_thread_whose_writer_holds_the_lock_is_open_and_the_probe_lets_go(tmp_path: Path) -> None:
    # given
    lock = _locks(tmp_path) / f"{THREAD}.lock"
    held = _hold(lock)

    # when
    while_held = CodexSessionActivity(str(tmp_path)).is_open(THREAD)
    os.close(held)
    after = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert (while_held, after) == (True, False)


def test_the_probe_never_creates_a_lock_file(tmp_path: Path) -> None:
    # given
    directory = _locks(tmp_path)

    # when
    CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert list(directory.iterdir()) == []


def test_an_unrelated_threads_writer_does_not_open_this_one(tmp_path: Path) -> None:
    # given
    held = _hold(_locks(tmp_path) / f"{OTHER}.lock")

    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)
    os.close(held)

    # then
    assert answer is False


def test_a_busy_coordination_lock_leaves_the_answer_unknown(tmp_path: Path, monkeypatch) -> None:
    # given — another writer is mid-way through taking locks
    directory = _locks(tmp_path)
    held = _hold(directory / ".coordination.lock")
    monkeypatch.setattr(module, "_COORDINATION_ATTEMPTS", 2)
    monkeypatch.setattr(module, "_COORDINATION_PAUSE", 0.001)

    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)
    os.close(held)

    # then
    assert answer is None


def test_a_free_coordination_lock_is_taken_and_released(tmp_path: Path) -> None:
    # given
    directory = _locks(tmp_path)
    (directory / ".coordination.lock").write_bytes(b"")
    (directory / f"{THREAD}.lock").write_bytes(b"")

    # when
    first = CodexSessionActivity(str(tmp_path)).is_open(THREAD)
    second = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then — released both times, or the second probe would have timed out
    assert (first, second) == (False, False)


@pytest.mark.parametrize("session", ["", "not-a-uuid", "../x", THREAD.upper()])
def test_an_id_that_is_not_a_thread_id_is_never_turned_into_a_path(tmp_path: Path, session: str) -> None:
    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(session)

    # then
    assert answer is None


def test_a_lock_folder_that_is_a_file_leaves_the_answer_unknown(tmp_path: Path) -> None:
    # given
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "thread-writer-locks").write_bytes(b"x")

    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert answer is None


def test_a_lock_file_that_is_a_symlink_is_not_followed(tmp_path: Path) -> None:
    # given
    (tmp_path / "real.lock").write_bytes(b"")
    (_locks(tmp_path) / f"{THREAD}.lock").symlink_to(tmp_path / "real.lock")

    # when
    answer = CodexSessionActivity(str(tmp_path)).is_open(THREAD)

    # then
    assert answer is None


# --------------------------------------------------------------------------- both agents together


class _Store:
    def __init__(self, owns: str, note: str | None = None, retired: bool = False) -> None:
        self._owns, self._note, self._retired = owns, note, retired

    def sessions(self, published: frozenset[str] = frozenset()) -> SessionListing:
        return SessionListing()

    def session_of(self, path: str) -> str | None:
        return "id" if path.startswith(self._owns) else None

    def note_for(self, path: str, data: bytes, *, created: bool) -> str | None:
        return self._note if path.startswith(self._owns) else None

    def retired(self, path: str) -> bool:
        return self._retired and path.startswith(self._owns)


def test_the_combined_store_asks_the_store_that_owns_the_path() -> None:
    # given
    combined = CombinedSessionStore((_Store("a/", "note-a"), _Store("b/", "note-b", retired=True)))

    # when
    answers = (
        combined.session_of("b/x"),
        combined.session_of("c/x"),
        combined.note_for("b/x", b"", created=True),
        combined.retired("b/x"),
        combined.retired("a/x"),
    )

    # then
    assert answers == ("id", None, "note-b", True, False)


class _Probe:
    def __init__(self, answer: bool | None) -> None:
        self._answer = answer

    def is_open(self, session_id: str) -> bool | None:
        return self._answer


@pytest.mark.parametrize(
    ("answers", "expected"),
    [((False, False), False), ((False, True), True), ((None, False), None), ((None, True), True), ((), False)],
)
def test_the_combined_probe_believes_open_before_unknown_before_closed(answers, expected) -> None:
    # given
    combined = CombinedSessionActivity(tuple(_Probe(a) for a in answers))

    # when
    answer = combined.is_open("id")

    # then
    assert answer is expected


# --------------------------------------------------------------------------- a published record claims its rollout


def test_a_rollout_already_published_under_the_project_is_listed_whatever_its_cwd_says(tmp_path: Path) -> None:
    # given — started on another Mac, so its first record names a path that is not this project's root
    home = _home(tmp_path)
    _rollout(home, _meta("/Users/someone/src/x"))
    published = frozenset({_logical(f"2026/11/09/{_name()}")})

    # when
    claimed, unclaimed = _store(home).sessions(published), _store(home).sessions()

    # then
    assert ([u.session_id for u in claimed.units], unclaimed.units) == ([THREAD], ())


def test_a_published_rollout_is_listed_even_when_its_head_cannot_be_read(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, b"not a record\n")
    published = frozenset({_logical(f"2026/11/09/{_name()}")})

    # when
    listing = _store(home).sessions(published)

    # then
    assert ([u.session_id for u in listing.units], listing.skipped) == ([THREAD], ())


def test_a_record_published_under_another_name_does_not_claim_the_rollout(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta("/Users/someone/src/x"))
    published = frozenset({f".codex/sessions/@other/2026/11/09/{_name()}"})

    # when
    listing = _store(home).sessions(published)

    # then
    assert listing.units == ()


def test_the_codex_layout_is_flat_and_the_claude_one_is_not() -> None:
    # given
    from syncintosh.core.agent_projects import AgentProjectSpec

    claude = AgentProjectSpec.create(home=".claude/projects", kind="claude")

    # when & then
    assert (CODEX_PROJECTS.flat, claude.flat) == (True, False)


def test_version_output_that_is_not_text_is_unknown_not_a_crash(monkeypatch) -> None:
    # given
    def fail(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(module.subprocess, "run", fail)

    # when
    version = module.installed_version()

    # then
    assert version is None


def test_a_name_with_non_ascii_digits_is_not_a_rollout_of_this_store(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    _rollout(home, _meta(_root(home)), day="٢٠٢٦/١١/٠٩")

    # when
    listing = _store(home).sessions()

    # then
    assert listing.units == ()


def test_a_rollout_that_is_a_fifo_is_not_read(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    target = home / ".codex" / "sessions" / "2026" / "11" / "09" / _name()
    target.parent.mkdir(parents=True)
    os.mkfifo(target)

    # when
    listing = _store(home).sessions()

    # then
    assert (listing.units, [s.reason for s in listing.skipped]) == ((), ["unknown"])
