# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: decide how the conflicts a stopped sync left pending are settled.

`resolve` runs the sync cycle again (``services/sync.py``, `Resolution`), so the conflicts it settles
are the ones that exist now, not the ones remembered from the stop. A decision is made against a
conflict's fingerprint and is never stored: it lives for this run only (``docs/design/01-sync-model.md``).

This module only builds decisions. The cycle, the publish, and the apply are `sync`'s.
"""

from __future__ import annotations

from collections.abc import Callable

from syncintosh.core.resolution import Choice, ConflictView, Decide, Picks, Side

# Asked once the decisions are made, before anything is published: the conflicts they settle and the
# picks. Returning `False` stops the run with nothing published.
Approve = Callable[[tuple[ConflictView, ...], Picks], bool]


def take_decision(side: Side, *, paths: tuple[str, ...] = (), approve: Approve | None = None) -> Decide:
    """A decision that sends every conflict, or each conflict on `paths`, to `side`.

    A delete/modify has no value on one side. "Take local" on a locally deleted record means
    deleting it, and "take remote" on a record the remote kept means keeping it.
    """

    def decide(views: tuple[ConflictView, ...]) -> Picks | None:
        picked = [view for view in views if not paths or view.path in paths]
        chosen = {view.fingerprint: _take(view, side) for view in picked}
        if not chosen:
            return None
        if approve is not None and not approve(tuple(picked), chosen):
            return None
        return chosen

    return decide


def _take(view: ConflictView, side: Side) -> Choice:
    if view.kind == "delete/modify":
        present = view.local_present if side == "local" else view.remote_present
        return "keep" if present else "delete"
    return side


__all__ = ["Approve", "take_decision"]
