# SPDX-License-Identifier: AGPL-3.0-or-later
"""`SyncReport.failed`: the one verdict `sync` and `resolve` exit on."""

from __future__ import annotations

import pytest

from syncintosh.core.models import PendingConflict
from syncintosh.services import Refusal, SyncReport

BLOCKING = Refusal(".zshrc", "the path's preset does not own it")
UNTRACKED = Refusal(".vscode", "the path is not in this machine's config", blocking=False)


@pytest.mark.parametrize(
    ("report", "failed"),
    [
        (SyncReport(), False),
        (SyncReport(applied=(".zshrc",)), False),
        (SyncReport(first_sight=(".zshrc",)), True),
        (SyncReport(refused=(BLOCKING,)), True),
        (SyncReport(unprojectable=(BLOCKING,)), True),
        (SyncReport(invalid=(BLOCKING,)), True),
        (SyncReport(refused=(UNTRACKED,)), False),
        (SyncReport(refused=(UNTRACKED, BLOCKING)), True),
    ],
)
def test_a_report_fails_on_a_conflict_a_first_sight_or_a_blocking_refusal(report, failed) -> None:
    # when & then
    assert report.failed is failed


def test_a_report_with_conflicts_fails() -> None:
    # given
    conflict = PendingConflict(file_id="f", path=".zshrc", detail="both sides changed")

    # when & then
    assert SyncReport(conflicts=(conflict,)).failed is True
