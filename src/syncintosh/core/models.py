# SPDX-License-Identifier: AGPL-3.0-or-later
"""Immutable domain models.

Boundary values are frozen dataclasses built once. Do not create an object and fill fields
afterwards (``AGENTS.md`` §2.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from syncintosh.core.tree import Absent, ElementId, KeyPath, Leaf, Tree, element_label

# Where a structural conflict sits: a key path, and the element of an identified array when the conflict is
# one binding under it (`None` for a conflict on the whole path).
ConflictAddress = tuple[KeyPath, ElementId | None]

# Whether a value applies to every machine or only to the current one.
Scope = Literal["common", "machine"]

# Which of the three merge inputs a value came from.
Side = Literal["base", "local", "remote"]


def _shape(value: Leaf) -> str:
    """A value's kind, for a conflict's repr — never the value itself (``AGENTS.md`` §4)."""
    return "absent" if isinstance(value, Absent) else type(value).__name__.lower()


@dataclass(frozen=True)
class Conflict:
    """A key path that was changed on both sides to different values.

    Only a both-sides change on the same path is a conflict; a one-sided change is applied
    automatically (``AGENTS.md`` §5.3). A value is ``ABSENT`` when that side has no leaf there —
    a key it never had, deleted, or replaced by an object whose children are the real change.
    """

    path: KeyPath
    base: Leaf
    local: Leaf
    remote: Leaf
    # Set when the conflict is one element of an array merged by identity (`core/keyed_array.py`);
    # `None` for a conflict on the whole path.
    element: ElementId | None = None

    def __repr__(self) -> str:
        # The sides are plaintext settings; a log or a traceback must not print them (§4). The
        # element identity is the field names and values that name it, never the array's content.
        return (
            f"Conflict(path={self.path!r}, element={None if self.element is None else element_label(self.element)}, "
            f"base=<{_shape(self.base)}>, "
            f"local=<{_shape(self.local)}>, remote=<{_shape(self.remote)}>)"
        )


@dataclass(frozen=True)
class TreeMerge:
    """The outcome of a three-way, key-level merge over two typed trees."""

    merged: Tree
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
    candidate) when it is True. `conflict_addresses` names where a structural merge found a conflict —
    a key path, and the element of an identified array when the conflict is one binding — never their
    values, so a pending report can say what collided. It is empty for a whole-file conflict (an
    undecodable, unparseable, or text merge).
    """

    data: bytes
    conflicted: bool
    conflict_addresses: tuple[ConflictAddress, ...] = ()


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
    """One unresolved record in a stopped sync, with the sides a resolver will need.

    `base`/`local`/`remote` are the plaintexts of the unresolved record; they live here, outside
    the repo, and nowhere else on disk (``AGENTS.md`` §3.3). `detail` is the human-facing reason.
    """

    file_id: str
    path: str
    detail: str
    base: bytes | None = None
    local: bytes | None = None
    remote: bytes | None = None

    def __repr__(self) -> str:
        # The sides are plaintext settings; keep them out of a log or a traceback.
        return (
            f"PendingConflict(file_id={self.file_id!r}, path={self.path!r}, detail={self.detail!r}, "
            f"base=<{_length(self.base)} bytes>, local=<{_length(self.local)} bytes>, "
            f"remote=<{_length(self.remote)} bytes>)"
        )


def _length(blob: bytes | None) -> int:
    return 0 if blob is None else len(blob)


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

    @classmethod
    def create(
        cls,
        *,
        local_commit: str,
        remote_commit: str,
        base_commit: str | None,
        conflicts: tuple[PendingConflict, ...],
    ) -> PendingState:
        """Validate the two commits a divergence always has, and freeze the conflicts.

        A stopped sync is always a divergence, so both the collect commit and the remote commit
        exist; an empty string would be indistinguishable from a real id.
        """
        if not local_commit or not remote_commit:
            raise ValueError("a pending divergence must name both its local and remote commit")
        if not conflicts:
            raise ValueError("a pending state must hold at least one conflict")
        return cls(
            local_commit=local_commit,
            remote_commit=remote_commit,
            base_commit=base_commit,
            conflicts=conflicts,
        )

    @property
    def has_conflicts(self) -> bool:
        """True when at least one record or path is unresolved."""
        return bool(self.conflicts)
