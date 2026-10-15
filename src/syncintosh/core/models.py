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
