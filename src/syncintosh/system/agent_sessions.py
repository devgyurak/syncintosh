# SPDX-License-Identifier: AGPL-3.0-or-later
"""One `SessionStore` and one `SessionActivity` over several agents (issue #56).

`sync` takes a single store and a single activity probe, and a machine may carry Claude Code and Codex sessions at once.
These combine the per-agent adapters without adding a rule: a record path belongs to exactly one agent (their homes
differ), so the store that recognises it answers, and an agent that does not know a session id answers `False`.

The activity answer is the cautious one: any agent that says a session is open wins, and any that cannot tell makes the
answer "cannot tell" — which every caller treats as open — before "closed" is ever believed.

Constructing these objects does no I/O (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

from syncintosh.core.ports import SessionActivity, SessionStore
from syncintosh.core.sessions import SessionListing


class CombinedSessionStore:
    """The sessions of every agent store, listed together."""

    def __init__(self, stores: tuple[SessionStore, ...]) -> None:
        self._stores = stores

    def sessions(self, published: frozenset[str] = frozenset()) -> SessionListing:
        """Every store's sessions, and everything each could not list safely."""
        listings = tuple(store.sessions(published) for store in self._stores)
        return SessionListing(
            units=tuple(unit for listing in listings for unit in listing.units),
            skipped=tuple(skipped for listing in listings for skipped in listing.skipped),
        )

    def session_of(self, path: str) -> str | None:
        """The session id from the first store that recognises `path`."""
        return next((found for store in self._stores if (found := store.session_of(path)) is not None), None)

    def note_for(self, path: str, data: bytes, *, created: bool) -> str | None:
        """The note from the store that owns `path`."""
        return next(
            (found for store in self._stores if (found := store.note_for(path, data, created=created)) is not None),
            None,
        )

    def retired(self, path: str) -> bool:
        """True when the store that owns `path` says the agent retired it."""
        return any(store.retired(path) for store in self._stores)


class CombinedSessionActivity:
    """Whether any agent has the session open."""

    def __init__(self, probes: tuple[SessionActivity, ...]) -> None:
        self._probes = probes

    def is_open(self, session_id: str) -> bool | None:
        """`True` if any probe says open, else `None` if any cannot tell, else `False`."""
        answers = tuple(probe.is_open(session_id) for probe in self._probes)
        if any(answer is True for answer in answers):
            return True
        return None if any(answer is None for answer in answers) else False


__all__ = ["CombinedSessionActivity", "CombinedSessionStore"]
