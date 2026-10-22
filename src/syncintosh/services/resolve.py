# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: decide how the conflicts a stopped sync left pending are settled.

`resolve` runs the sync cycle again (``services/sync.py``, `Resolution`), so the conflicts it settles
are the ones that exist now, not the ones remembered from the stop. A decision is made against a
conflict's fingerprint and is never stored: it lives for this run only (``docs/design/01-sync-model.md``).

Every way of deciding goes through one `decision`. A choice source (`take_chooser`, `scoped_chooser`)
only says what one conflict's choice is, or that it has none.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from syncintosh.core.errors import ConflictsNotCovered
from syncintosh.core.paths import collision_key
from syncintosh.core.resolution import Choice, ConflictView, Decide, Picks, Side

# Asked once the decisions are made, before anything is published: the conflicts and every pick the
# run would publish. Returning `False` stops the run with nothing published.
Approve = Callable[[tuple[ConflictView, ...], Picks], bool]

# Settles one conflict, or returns `None` when it has no choice for that conflict.
Chooser = Callable[[ConflictView], Choice | None]


def decision(choose: Chooser, *, approve: Approve | None = None) -> Decide:
    """The decision every resolve run uses.

    A conflict answered earlier in the run is not asked again. A conflict `choose` has no choice for
    stops the run before anything is published, and the error names what is left, so nothing is
    published on a partial decision. `approve` sees the conflicts and every pick the run would publish.
    """

    def decide(views: tuple[ConflictView, ...], answered: Picks) -> Picks | None:
        fresh: dict[str, Choice] = {}
        uncovered: list[ConflictView] = []
        for view in views:
            if view.fingerprint in answered:
                continue
            choice = choose(view)
            if choice is None:
                uncovered.append(view)
            else:
                fresh[view.fingerprint] = choice
        if uncovered:
            raise ConflictsNotCovered(_describe(uncovered))
        if not fresh:
            return None
        if approve is not None and not approve(views, {**answered, **fresh}):
            return None
        return fresh

    return decide


def take_chooser(side: Side) -> Chooser:
    """Every conflict goes to `side`."""

    def choose(view: ConflictView) -> Choice | None:
        return _take(view, side)

    return choose


def scoped_chooser(local: tuple[str, ...], remote: tuple[str, ...], *, home: str) -> Chooser:
    """Each conflict named by a path goes to that path's side; a conflict no path names has no choice."""
    sides: dict[str, Side] = {}
    for side, paths in (("local", local), ("remote", remote)):
        for raw in paths:
            key = collision_key(normalize_path(raw, home))
            if sides.get(key, side) != side:
                raise ValueError(f"{raw} is named for both sides")
            sides[key] = side

    def choose(view: ConflictView) -> Choice | None:
        return sides.get(collision_key(view.path))

    return choose


def normalize_path(raw: str, home: str) -> str:
    """A path as a conflict records it: relative to home, with `~` and an absolute home prefix removed.

    A relative path is taken as already relative to home. A shell that expanded `~` gives the absolute
    form, which is brought back to the same relative path.
    """
    # `~` means the home this run is for, not whatever HOME the process happens to have.
    expanded = raw[2:] if raw.startswith("~/") else raw
    if not Path(expanded).is_absolute():
        return str(PurePosixPath(expanded))
    prefix = home.rstrip("/") + "/"
    return expanded[len(prefix) :] if expanded.startswith(prefix) else expanded


def _take(view: ConflictView, side: Side) -> Choice:
    if view.kind == "delete/modify":
        present = view.local_present if side == "local" else view.remote_present
        return "keep" if present else "delete"
    return side


def _describe(views: list[ConflictView]) -> str:
    return ", ".join(f"{view.path} ({view.kind})" for view in views)


__all__ = ["Approve", "Chooser", "decision", "normalize_path", "scoped_chooser", "take_chooser"]
