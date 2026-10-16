# SPDX-License-Identifier: AGPL-3.0-or-later
"""Immutable domain models.

Boundary values are frozen dataclasses built once. Do not create an object and fill fields
afterwards (``AGENTS.md`` §2.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Whether a value applies to every machine or only to the current one.
Scope = Literal["common", "machine"]

# Which of the three merge inputs a value came from.
Side = Literal["base", "local", "remote"]


@dataclass(frozen=True)
class Conflict:
    """A key that was changed on both sides to different values.

    Only a both-sides change on the same key is a conflict; a one-sided change is applied
    automatically (``AGENTS.md`` §5.3).
    """

    key: str
    base: str | None
    local: str | None
    remote: str | None


@dataclass(frozen=True)
class MergeResult:
    """The outcome of a three-way, key-level merge."""

    merged: dict[str, str]
    conflicts: tuple[Conflict, ...]

    @property
    def has_conflicts(self) -> bool:
        """True when at least one key needs a human (or LLM) decision."""
        return bool(self.conflicts)


@dataclass(frozen=True)
class TextMerge:
    """The outcome of a line-level three-way text merge.

    `conflicted` is decided by the merge itself, never inferred afterwards from conflict
    markers in `text`: a file whose real content happens to contain a marker line must not be
    reported as conflicted (``docs/design/01-sync-model.md``).
    """

    text: str
    conflicted: bool


@dataclass(frozen=True)
class ContentMerge:
    """The outcome of a byte-level content merge for one record.

    `data` is the merged bytes when `conflicted` is False, and the local side (an unresolved
    candidate) when it is True.
    """

    data: bytes
    conflicted: bool


@dataclass(frozen=True)
class LedgerEntry:
    """One file's apply-ledger baseline: the digest this machine has accounted for.

    The ledger lives outside the repo (``AGENTS.md`` §3.3) and is the sole basis for change
    detection, so a comparison against `HEAD` never misreads this machine's own pre-apply
    files as local edits (``docs/design/01-sync-model.md``).
    """

    sha256: str
    commit: str
    suppressed: bool


@dataclass(frozen=True)
class PendingConflict:
    """One unresolved record in a stopped sync, described for the user."""

    file_id: str
    path: str
    detail: str


@dataclass(frozen=True)
class PendingState:
    """The unresolved merge a stopped sync leaves behind.

    Holds the two commits that diverged and every conflict, so a later `resolve` re-runs the
    cycle from the collect step instead of guessing (``docs/design/01-sync-model.md``).
    """

    local_commit: str
    remote_commit: str
    base_commit: str | None
    conflicts: tuple[PendingConflict, ...]

    @property
    def has_conflicts(self) -> bool:
        """True when at least one record or path is unresolved."""
        return bool(self.conflicts)
