# SPDX-License-Identifier: AGPL-3.0-or-later
"""One `SessionStore` and one `SessionActivity` over several agents (issue #56).

`sync` takes a single store and a single activity probe, and a machine may carry Claude Code and Codex sessions at once.
These combine the per-agent adapters without adding a rule: a record path belongs to exactly one agent (their homes
differ), so the store that recognises it answers, and an agent that does not know a session id answers `False`.

The activity question goes to the agent that **owns** the session, as the store learned when it listed or recognised
it: one agent's unreadable state must not hold back the other agent's sessions. A session no store has claimed yet is
asked of every agent, and the cautious answer wins: any agent that says open wins, and any that cannot tell makes the
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
        # Which store a session id came from, learned as the stores list and recognise sessions during one cycle.
        self._owners: dict[str, int] = {}

    def owner_of(self, session_id: str) -> int | None:
        """The position of the store that owns `session_id`, or `None` when none has claimed it yet."""
        return self._owners.get(session_id)

    def sessions(self, published: frozenset[str] = frozenset()) -> SessionListing:
        """Every store's sessions, and everything each could not list safely."""
        listings = tuple(store.sessions(published) for store in self._stores)
        self._owners.update(
            {unit.session_id: index for index, listing in enumerate(listings) for unit in listing.units}
        )
        return SessionListing(
            units=tuple(unit for listing in listings for unit in listing.units),
            skipped=tuple(skipped for listing in listings for skipped in listing.skipped),
        )

    def session_of(self, path: str) -> str | None:
        """The session id from the first store that recognises `path`."""
        for index, store in enumerate(self._stores):
            if (found := store.session_of(path)) is not None:
                self._owners[found] = index
                return found
        return None

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

    def __init__(self, store: CombinedSessionStore, probes: tuple[SessionActivity, ...]) -> None:
        self._store = store
        self._probes = probes

    def is_open(self, session_id: str) -> bool | None:
        """The owner's answer; for an unclaimed id, open if any probe says so, else unknown if any cannot tell."""
        owner = self._store.owner_of(session_id)
        if owner is not None:
            return self._probes[owner].is_open(session_id)
        answers = tuple(probe.is_open(session_id) for probe in self._probes)
        if any(answer is True for answer in answers):
            return True
        return None if any(answer is None for answer in answers) else False


__all__ = ["CombinedSessionActivity", "CombinedSessionStore"]
