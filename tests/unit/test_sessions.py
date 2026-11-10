# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for an agent session: its shape, when it may be sealed, and the caps (issue #56, decisions D6 and D8)."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import ConfigError
from syncintosh.core.sessions import (
    IDLE_SECONDS,
    MAX_FILE_BYTES,
    MAX_PROJECT_BYTES,
    MIB,
    SessionFile,
    SessionSkipped,
    SessionUnit,
    eligibility,
    within_project_cap,
)

NOW = 1_000_000.0
OLD = NOW - IDLE_SECONDS - 1


def _file(name: str = "a.jsonl", *, size: int = 10, mtime: float = OLD) -> SessionFile:
    return SessionFile.create(path=f"x/@p/{name}", live=f"x/J0/{name}", size=size, mtime=mtime)


def _unit(session: str = "s1", project: str = "p", *files: SessionFile) -> SessionUnit:
    return SessionUnit.create(session_id=session, project=project, files=files or (_file(),))


def test_thirty_minutes_and_a_48_mib_file_are_the_limits() -> None:
    # when & then
    assert (IDLE_SECONDS, MAX_FILE_BYTES, MAX_PROJECT_BYTES) == (1800, 48 * MIB, 256 * MIB)


@pytest.mark.parametrize(
    ("changes"),
    [
        {"path": "/abs"},
        {"live": "a/../b"},
        {"size": -1},
        {"size": True},
        {"mtime": -1.0},
        {"mtime": "x"},
    ],
)
def test_a_session_file_with_a_bad_field_is_refused(changes: dict) -> None:
    # given
    fields = {"path": "a", "live": "b", "size": 1, "mtime": 1.0, **changes}

    # when & then
    with pytest.raises(ConfigError):
        SessionFile.create(**fields)


@pytest.mark.parametrize(
    ("session", "project", "files"),
    [("", "p", (_file(),)), ("s", "", (_file(),)), ("s", "p", ()), ("s", "p", (_file(), _file()))],
)
def test_a_session_must_name_itself_its_project_and_distinct_files(session, project, files) -> None:
    # when & then
    with pytest.raises(ConfigError):
        SessionUnit.create(session_id=session, project=project, files=files)


def test_a_session_adds_up_its_files_and_takes_the_newest_mtime() -> None:
    # given
    unit = _unit("s", "p", _file("a", size=3, mtime=5.0), _file("b", size=4, mtime=9.0))

    # when & then
    assert (unit.size, unit.newest) == (7, 9.0)


@pytest.mark.parametrize(
    ("is_open", "mtime", "size", "expected"),
    [
        (False, OLD, 10, None),
        (None, OLD, 10, "open"),
        (True, OLD, 10, "open"),
        (False, NOW - IDLE_SECONDS + 1, 10, "open"),
        (False, NOW, 10, "open"),
        (False, OLD, MAX_FILE_BYTES, None),
        (False, OLD, MAX_FILE_BYTES + 1, "cap"),
    ],
)
def test_a_session_is_sealed_only_when_closed_idle_and_within_the_file_cap(
    is_open: bool | None, mtime: float, size: int, expected: str | None
) -> None:
    # given
    unit = _unit("s", "p", _file(size=size, mtime=mtime))

    # when
    why = eligibility(unit, now=NOW, is_open=is_open)

    # then
    assert why == expected


def test_one_young_file_keeps_the_whole_session_open() -> None:
    # given
    unit = _unit("s", "p", _file("a", mtime=OLD), _file("b", mtime=NOW))

    # when
    why = eligibility(unit, now=NOW, is_open=False)

    # then
    assert why == "open"


def _big(session: str, mtime: float, project: str = "p") -> SessionUnit:
    return _unit(session, project, _file(f"{session}.jsonl", size=100 * MIB, mtime=mtime))


def test_the_newest_new_session_is_the_one_held_back_by_the_project_cap() -> None:
    # given — 100 MiB each: two fit under 256 MiB, the third does not
    units = (_big("c", 3.0), _big("a", 1.0), _big("b", 2.0))

    # when
    kept, over = within_project_cap(units)

    # then
    assert ([u.session_id for u in kept], [u.session_id for u in over]) == (["a", "b"], ["c"])


def test_a_published_session_is_kept_even_when_the_project_is_over_the_cap() -> None:
    # given
    units = (_big("a", 1.0), _big("b", 2.0), _big("c", 3.0))

    # when
    kept, over = within_project_cap(units, published=frozenset({"c"}))

    # then — `c` is counted first and cannot be dropped; only one more of the others fits
    assert ([u.session_id for u in kept], [u.session_id for u in over]) == (["c", "a"], ["b"])


def test_each_project_has_its_own_cap() -> None:
    # given
    units = (_big("a", 1.0, "one"), _big("b", 2.0, "one"), _big("c", 3.0, "two"))

    # when
    _, over = within_project_cap(units)

    # then
    assert over == ()


def test_the_cap_decision_is_the_same_on_every_cycle() -> None:
    # given
    units = (_big("c", 3.0), _big("a", 1.0), _big("b", 2.0))

    # when
    first, second = within_project_cap(units), within_project_cap(tuple(reversed(units)))

    # then
    assert first == second


@pytest.mark.parametrize("reason", ["secret", "cap", "forked", "open", "unknown"])
def test_a_skip_report_carries_a_fixed_phrase_by_default(reason: str) -> None:
    # when
    skipped = SessionSkipped.create(session_id="s", reason=reason)  # ty: ignore[invalid-argument-type]

    # then
    assert (skipped.reason, skipped.detail != "") == (reason, True)


def test_a_skip_report_keeps_the_scanners_location_text() -> None:
    # when
    skipped = SessionSkipped.create(session_id="s", reason="secret", path="p", detail="p: possible AWS key at line 3")

    # then
    assert skipped.detail == "p: possible AWS key at line 3"
