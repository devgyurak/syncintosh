# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-record classification against the apply ledger, and the first-connect choice.

Change detection is against the **apply ledger**, never `HEAD`: apply runs after the push, so
between the push and the apply `HEAD` already holds content this machine has not written, and a
comparison against `HEAD` would read this machine's own still-old files as local edits and
silently revert the other machine's change (``docs/design/01-sync-model.md``).

This module is pure — it takes plain digests and returns a verdict — so the table in the design
note is testable case by case (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from typing import Literal

from syncintosh.core.models import LedgerEntry

# The six outcomes of the step-4 table. A value is serialized/compared verbatim, so it is a
# `Literal`, never an `Enum` (``AGENTS.md`` §4).
LedgerClassification = Literal[
    "deferred",
    "first-sight",
    "suppressed",
    "in-sync",
    "pending-apply",
    "local-change",
]

# The first-connect choice for a record that has no usable base on this machine
# (``docs/design/02-base-and-merge.md``). The chosen side replaces the whole file and keeps the
# record's `file_id`.
AdoptChoice = Literal["local", "remote"]


def classify(entry: LedgerEntry | None, live_sha256: str | None, head_sha256: str | None) -> LedgerClassification:
    """Classify one record from its ledger entry, its live digest, and its `HEAD` digest.

    `entry` is `None` when this machine has no baseline; `live_sha256` is `None` when the file
    is missing; `head_sha256` is the record's digest in the commit being compared. A missing live
    file is never a deletion — it means "not applied here" (the app is not installed, or the user
    removed it), and the record survives either way.
    """
    if entry is None:
        if live_sha256 is None:
            return "deferred"
        if head_sha256 is not None and live_sha256 == head_sha256:
            return "in-sync"
        return "first-sight"
    if entry.suppressed:
        if live_sha256 is None:
            return "suppressed"
        if head_sha256 is not None and live_sha256 == head_sha256:
            return "in-sync"
        # A reinstalled app writes a fresh default; collecting it would let those defaults win
        # the content merge against the other machines' settings with no conflict.
        return "first-sight"
    if live_sha256 is None:
        # Present baseline, missing file: the app was uninstalled, or the user removed it
        # deliberately. Never recreate it on every sync.
        return "suppressed"
    if live_sha256 != entry.sha256:
        return "local-change"
    if head_sha256 == entry.sha256:
        return "in-sync"
    return "pending-apply"


def chosen_digest(choice: AdoptChoice, *, local_sha256: str, remote_sha256: str) -> str:
    """The digest of the side the user adopted for a first-sight record."""
    return local_sha256 if choice == "local" else remote_sha256


__all__ = ["AdoptChoice", "LedgerClassification", "chosen_digest", "classify"]
