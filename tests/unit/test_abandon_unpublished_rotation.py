# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for sync's recovery from a local rotation the remote never saw.

A local-only hygiene rotation is dropped (the remote tip is adopted, the stray generation leaves the
ring, and the cycle restarts); an out-of-band one is refused; a generation the remote does publish is
left to the normal checks (``docs/design/05-key-generations.md``).
"""

from __future__ import annotations

import sys
from typing import cast

import pytest

# `syncintosh.services.sync` is also a function name in the package, so the module is taken from `sys.modules`.
import syncintosh.services.sync  # noqa: F401  (imported for the side effect of registering the module)
from syncintosh.core.errors import GenerationDivergence
from syncintosh.core.ports import RepoFiles
from syncintosh.core.repo import wrap_path
from syncintosh.services.keys import discard_stray_generations

_SYNC = sys.modules["syncintosh.services.sync"]

LOCAL = "l" * 40
REMOTE = "r" * 40
KEY = b"\x07" * 32
PUBLISHED = {1: "c1"}


class _Transport:
    """The `PolicyView` calls the recovery makes, over a fixed commit graph."""

    def __init__(self, *, head: str | None = LOCAL, ancestors: frozenset[tuple[str, str]] = frozenset(), wraps=()):
        self._head = head
        self._ancestors = ancestors
        self._wraps = set(wraps)
        self.moved: list[tuple[str, str]] = []
        self.materialized: list[str] = []

    def head(self) -> str | None:
        return self._head

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return (ancestor, descendant) in self._ancestors

    def read_blob(self, commit: str, relpath: str) -> bytes | None:
        return b"wrap" if (commit, relpath) in self._wraps else None

    def update_ref(self, branch: str, commit: str) -> None:
        self.moved.append((branch, commit))

    def materialize(self, commit: str) -> None:
        self.materialized.append(commit)


class _Files:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, relpath: str) -> None:
        self.deleted.append(relpath)


def _abandon(transport, files, store, *, published=PUBLISHED, local_generation=2):
    return _SYNC._abandon_unpublished_rotation(
        transport=transport,
        files=cast(RepoFiles, files),
        store=store,
        branch="main",
        remote_commit=REMOTE,
        published=published,
        local_generation=local_generation,
    )


def test_a_hygiene_rotation_the_remote_diverged_from_is_adopted_away(memory_key_store) -> None:
    # given — local generation 2 has a wrap in its tree, the remote publishes only generation 1
    store = memory_key_store({1: KEY, 2: KEY})
    transport = _Transport(wraps={(LOCAL, wrap_path(2))})
    files = _Files()

    # when
    outcome = _abandon(transport, files, store)

    # then — the remote tip is adopted, the stray generation and its wrap are gone, the cycle restarts
    assert outcome is not None
    assert (outcome.restart, outcome.remote_commit) == (True, REMOTE)
    assert outcome.note == _SYNC.ABANDONED_ROTATION_NOTE.format(generation=2)
    assert (transport.moved, transport.materialized) == ([("main", REMOTE)], [REMOTE])
    assert (store.generations(), files.deleted) == ((1,), [wrap_path(2)])


def test_an_out_of_band_rotation_is_refused_and_left_alone(memory_key_store) -> None:
    # given — no wrap in the local tree: the key was never meant to travel through the repo
    store = memory_key_store({1: KEY, 2: KEY})
    transport = _Transport()
    files = _Files()

    # when & then
    with pytest.raises(GenerationDivergence, match="--out-of-band"):
        _abandon(transport, files, store)
    assert (transport.moved, store.generations(), files.deleted) == ([], (1, 2), [])


@pytest.mark.parametrize(
    ("published", "local_generation", "ancestors", "head"),
    [
        ({1: "c1", 2: "c2"}, 2, frozenset(), LOCAL),
        ({1: "c1", 2: "c2", 3: "c3"}, 2, frozenset(), LOCAL),
        (PUBLISHED, 1, frozenset(), LOCAL),
        (PUBLISHED, 2, frozenset({(REMOTE, LOCAL)}), LOCAL),
        (PUBLISHED, 2, frozenset({(LOCAL, REMOTE)}), LOCAL),
        (PUBLISHED, 2, frozenset(), REMOTE),
        (PUBLISHED, 2, frozenset(), None),
    ],
    ids=[
        "published_generation",
        "remote_is_ahead_in_generation",
        "same_generation",
        "local_is_ahead_of_remote",
        "local_is_behind_remote",
        "already_on_the_remote_tip",
        "unborn_branch",
    ],
)
def test_anything_else_is_left_to_the_normal_checks(
    memory_key_store, published, local_generation, ancestors, head
) -> None:
    # given
    store = memory_key_store({1: KEY, 2: KEY})
    transport = _Transport(head=head, ancestors=ancestors, wraps={(LOCAL, wrap_path(2))})
    files = _Files()

    # when
    outcome = _abandon(transport, files, store, published=published, local_generation=local_generation)

    # then — nothing moved and nothing dropped
    assert outcome is None
    assert (transport.moved, store.generations(), files.deleted) == ([], (1, 2), [])


def test_discard_stray_generations_drops_only_newer_generations_and_their_wraps(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY, 2: KEY, 3: KEY, 4: KEY})
    files = _Files()

    # when
    discard_stray_generations(files=cast(RepoFiles, files), store=store, above=2)

    # then
    assert (store.generations(), files.deleted) == ((1, 2), [wrap_path(3), wrap_path(4)])


def test_the_out_of_band_divergence_names_the_command_to_rerun() -> None:
    # when
    message = str(GenerationDivergence(3, 2, out_of_band=True))

    # then — and says why sync will not discard it on its own
    assert "syncintosh key rotate --out-of-band" in message
    assert "device removal" in message


def test_the_plain_divergence_message_is_unchanged() -> None:
    # when
    message = str(GenerationDivergence(3, 2))

    # then
    assert "diverges from remote 2" in message
    assert "syncintosh key rotate`" in message
