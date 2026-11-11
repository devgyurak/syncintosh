# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field policy of a collect-built `SyncReport`: observed always shows, done only after the commit."""

from __future__ import annotations

import pytest

from syncintosh.core.sessions import SessionSkipped
from syncintosh.services.sync._shared import Deferral, Refusal, SyncReport, _Collect, _collect_report

OBSERVED = ("symlinks", "skipped", "refused", "unprojectable", "deferred", "session_skipped", "notes")
RESULTS = ("created", "collected", "suppressed")


def _populated() -> _Collect:
    """A collect pass that found something of every kind."""
    return _Collect(
        created=[".new"],
        collected=[".zshrc"],
        suppressed=[".noise"],
        symlinks=[".link"],
        skipped=[".skipped"],
        refused=[Refusal(".secret", "a dangerous path this machine never collects")],
        unprojectable=[Refusal(".odd", "the file is not UTF-8")],
        deferred=[Deferral(".app", "the app is not installed on this machine")],
        session_skipped=[SessionSkipped.create(session_id="s1", reason="open")],
    )


def _filled(report: SyncReport, fields: tuple[str, ...]) -> list[str]:
    return [name for name in fields if getattr(report, name)]


@pytest.mark.parametrize("committed", [False, True])
def test_what_collect_observed_is_reported_whether_or_not_a_commit_was_made(committed) -> None:
    # given
    collected = _populated()

    # when
    report = _collect_report(collected, committed=committed, notes=("a note",))

    # then
    assert _filled(report, OBSERVED) == list(OBSERVED)


@pytest.mark.parametrize(("committed", "expected"), [(False, []), (True, list(RESULTS))])
def test_what_a_cycle_did_is_reported_only_after_the_collect_commit(committed, expected) -> None:
    # given
    collected = _populated()

    # when
    report = _collect_report(collected, committed=committed, notes=())

    # then — a return before the commit must not claim files as collected
    assert _filled(report, RESULTS) == expected


def test_a_later_stage_replaces_what_collect_found() -> None:
    # given
    collected = _populated()

    # when
    report = _collect_report(collected, committed=True, notes=("n",), refused=())

    # then
    assert report.refused == ()
