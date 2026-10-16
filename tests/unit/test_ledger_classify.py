# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the step-4 classification table and the first-connect choice."""

from __future__ import annotations

import pytest

from syncintosh.core.ledger import chosen_digest, classify
from syncintosh.core.models import LedgerEntry

LEDGER_SHA = "a" * 64
OTHER_SHA = "b" * 64
HEAD_SHA = "c" * 64
COMMIT = "d" * 40

ENTRY = LedgerEntry(sha256=LEDGER_SHA, commit=COMMIT, suppressed=False)
SUPPRESSED = LedgerEntry(sha256=LEDGER_SHA, commit=COMMIT, suppressed=True)


@pytest.mark.parametrize(
    ("entry", "live_sha256", "head_sha256", "expected"),
    [
        # absent ledger, missing file → deferred (typically the app is not installed)
        (None, None, HEAD_SHA, "deferred"),
        (None, None, None, "deferred"),
        # absent ledger, present file → first sight, or in sync when it already matches
        (None, OTHER_SHA, HEAD_SHA, "first-sight"),
        (None, HEAD_SHA, HEAD_SHA, "in-sync"),
        (None, HEAD_SHA, None, "first-sight"),
        # present baseline, missing file → locally suppressed (never a deletion)
        (ENTRY, None, HEAD_SHA, "suppressed"),
        # still suppressed
        (SUPPRESSED, None, HEAD_SHA, "suppressed"),
        # a suppressed file that came back → first sight again
        (SUPPRESSED, OTHER_SHA, HEAD_SHA, "first-sight"),
        (SUPPRESSED, HEAD_SHA, HEAD_SHA, "in-sync"),
        # the plain rows
        (ENTRY, LEDGER_SHA, LEDGER_SHA, "in-sync"),
        (ENTRY, LEDGER_SHA, HEAD_SHA, "pending-apply"),
        (ENTRY, LEDGER_SHA, None, "pending-apply"),
        (ENTRY, OTHER_SHA, HEAD_SHA, "local-change"),
        (ENTRY, OTHER_SHA, LEDGER_SHA, "local-change"),
    ],
)
def test_the_classification_table(entry, live_sha256, head_sha256, expected: str) -> None:
    # when
    verdict = classify(entry, live_sha256, head_sha256)

    # then
    assert verdict == expected


def test_adopting_local_keeps_the_local_digest() -> None:
    # when
    digest = chosen_digest("local", local_sha256=OTHER_SHA, remote_sha256=HEAD_SHA)

    # then
    assert digest == OTHER_SHA


def test_adopting_remote_takes_the_repo_digest() -> None:
    # when
    digest = chosen_digest("remote", local_sha256=OTHER_SHA, remote_sha256=HEAD_SHA)

    # then
    assert digest == HEAD_SHA
