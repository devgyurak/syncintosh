# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for sync's recovery from a local rotation the remote never saw.

Only a rotation `key rotate` marked as pending (its push outcome was never learned) can be dropped: the
remote tip is adopted, the stray generation leaves the ring, the marker is cleared and the cycle restarts.
An out-of-band one is refused; with no marker, or for a generation the remote does publish, the normal
checks decide (``docs/design/05-key-generations.md``).
"""

from __future__ import annotations

import sys
from typing import cast

import pytest

# `syncintosh.services.sync` is also a function name in the package, so the module is taken from `sys.modules`.
import syncintosh.services.sync  # noqa: F401  (imported for the side effect of registering the module)
from syncintosh.core.errors import GenerationDivergence
from syncintosh.core.models import PendingPush
from syncintosh.core.ports import RepoFiles, RingCodec
from syncintosh.core.repo import wrap_path
from syncintosh.services.keys import discard_stray_generations

_SYNC = sys.modules["syncintosh.services.sync"]

LOCAL = "l" * 40
REMOTE = "r" * 40
ROTATION = "9" * 40
KEY = b"\x07" * 32
PUBLISHED = {1: "c1"}
MARKER = PendingPush.create(generation=2, commit=ROTATION)
# The rotation commit is in the local history, and the remote diverged from it.
DIVERGED = frozenset({(ROTATION, LOCAL)})
# The remote is an ancestor of the local branch: the rotation was never pushed.
AHEAD = frozenset({(ROTATION, LOCAL), (REMOTE, LOCAL)})


class _Transport:
    """The `PolicyView` calls the recovery makes, over a fixed commit graph."""

    def __init__(self, *, head: str | None = LOCAL, ancestors: frozenset[tuple[str, str]] = DIVERGED, wraps=()):
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


class _Marker:
    def __init__(self, marker: PendingPush | None = MARKER) -> None:
        self.marker = marker
        self.cleared = 0

    def load(self) -> PendingPush | None:
        return self.marker

    def record(self, marker: PendingPush) -> None:
        self.marker = marker

    def clear(self) -> None:
        self.marker = None
        self.cleared += 1


def _abandon(transport, files, store, markers, *, published=PUBLISHED, marker=MARKER, local_generation=2):
    return _SYNC._abandon_unpublished_rotation(
        transport=transport,
        files=cast(RepoFiles, files),
        store=store,
        rotation_marker=markers,
        branch="main",
        remote_commit=REMOTE,
        published=published,
        marker=marker,
        local_generation=local_generation,
    )


@pytest.mark.parametrize("ancestors", [DIVERGED, AHEAD], ids=["diverged", "ahead"])
def test_a_marked_hygiene_rotation_the_remote_never_saw_is_adopted_away(memory_key_store, ancestors) -> None:
    # given — local generation 2 has a wrap in its tree, the remote publishes only generation 1
    store = memory_key_store({1: KEY, 2: KEY})
    transport = _Transport(ancestors=ancestors, wraps={(LOCAL, wrap_path(2))})
    files = _Files()
    markers = _Marker()

    # when
    outcome = _abandon(transport, files, store, markers)

    # then — the remote tip is adopted, the stray generation, its wrap and the marker are gone, the cycle restarts
    assert outcome is not None
    assert (outcome.restart, outcome.remote_commit) == (True, REMOTE)
    assert outcome.note == _SYNC.ABANDONED_ROTATION_NOTE.format(generation=2)
    assert (transport.moved, transport.materialized) == ([("main", REMOTE)], [REMOTE])
    assert (store.generations(), files.deleted, markers.marker) == ((1,), [wrap_path(2)], None)


@pytest.mark.parametrize("ancestors", [DIVERGED, AHEAD], ids=["diverged", "ahead"])
def test_a_marked_out_of_band_rotation_is_refused_and_left_alone(memory_key_store, ancestors) -> None:
    # given — no wrap in the local tree: the key was never meant to travel through the repo
    store = memory_key_store({1: KEY, 2: KEY})
    transport = _Transport(ancestors=ancestors)
    files = _Files()
    markers = _Marker()

    # when & then
    with pytest.raises(GenerationDivergence, match="--out-of-band"):
        _abandon(transport, files, store, markers)
    assert (transport.moved, store.generations(), files.deleted, markers.marker) == ([], (1, 2), [], MARKER)


@pytest.mark.parametrize(
    ("published", "marker", "local_generation", "ancestors", "head"),
    [
        (PUBLISHED, None, 2, DIVERGED, LOCAL),
        (PUBLISHED, PendingPush.create(generation=3, commit=ROTATION), 2, DIVERGED, LOCAL),
        ({}, MARKER, 2, DIVERGED, LOCAL),
        ({1: "c1", 2: "c2"}, MARKER, 2, DIVERGED, LOCAL),
        ({1: "c1", 2: "c2", 3: "c3"}, MARKER, 2, DIVERGED, LOCAL),
        (PUBLISHED, PendingPush.create(generation=1 + 1, commit="8" * 40), 2, DIVERGED, LOCAL),
        (PUBLISHED, MARKER, 2, frozenset({(ROTATION, LOCAL), (LOCAL, REMOTE)}), LOCAL),
        (PUBLISHED, MARKER, 2, DIVERGED, REMOTE),
        (PUBLISHED, MARKER, 2, DIVERGED, None),
        ({1: "c1", 2: "c2"}, PendingPush.create(generation=2, commit=ROTATION), 1, DIVERGED, LOCAL),
    ],
    ids=[
        "no_marker",
        "marker_for_another_generation",
        "empty_published",
        "published_generation",
        "remote_ahead_in_generation",
        "marker_commit_not_in_local_history",
        "local_is_behind_remote",
        "already_on_the_remote_tip",
        "unborn_branch",
        "local_generation_is_not_the_marked_one",
    ],
)
def test_anything_else_is_left_to_the_normal_checks(
    memory_key_store, published, marker, local_generation, ancestors, head
) -> None:
    # given
    store = memory_key_store({1: KEY, 2: KEY})
    transport = _Transport(head=head, ancestors=ancestors, wraps={(LOCAL, wrap_path(2))})
    files = _Files()
    markers = _Marker(marker)

    # when
    outcome = _abandon(
        transport, files, store, markers, published=published, marker=marker, local_generation=local_generation
    )

    # then — nothing moved, nothing dropped, the marker untouched
    assert outcome is None
    assert (transport.moved, store.generations(), files.deleted, markers.cleared) == ([], (1, 2), [], 0)


class _Codec:
    def commitment(self, key: bytes) -> str:
        return "c" + key.hex()[:2]


@pytest.mark.parametrize(
    ("published", "held", "expected"),
    [
        ({2: "c07"}, KEY, True),
        ({2: "other"}, KEY, False),
        ({1: "c07"}, KEY, False),
        ({2: "c07"}, None, False),
    ],
    ids=["same_key", "different_commitment", "generation_not_published", "key_not_held"],
)
def test_a_marked_rotation_landed_only_when_the_remote_publishes_this_machines_key(
    memory_key_store, published, held, expected
) -> None:
    # given
    store = memory_key_store({1: KEY} if held is None else {1: KEY, 2: held})

    # when
    landed = _SYNC._landed(cast(RingCodec, _Codec()), store, MARKER, published)

    # then
    assert landed is expected


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


def test_a_held_generation_the_remote_does_not_publish_is_not_swept_on_the_markers_account(memory_key_store) -> None:
    # given — generation 2 is held but unpublished too (a forged or truncated remote map), marker is for 3
    store = memory_key_store({1: KEY, 2: KEY, 3: KEY})
    marker = PendingPush.create(generation=3, commit=ROTATION)
    transport = _Transport(wraps={(LOCAL, wrap_path(3))})
    files = _Files()
    markers = _Marker(marker)

    # when
    outcome = _abandon(transport, files, store, markers, marker=marker, local_generation=3)

    # then — left to `verify_commitments`: nothing moved, nothing dropped
    assert outcome is None
    assert (transport.moved, store.generations(), files.deleted, markers.cleared) == ([], (1, 2, 3), [], 0)


def test_only_the_marked_generation_is_swept_when_the_others_are_published(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY, 2: KEY, 3: KEY})
    marker = PendingPush.create(generation=3, commit=ROTATION)
    transport = _Transport(wraps={(LOCAL, wrap_path(3))})
    files = _Files()

    # when
    _abandon(transport, files, store, _Marker(marker), published={1: "c1", 2: "c2"}, marker=marker, local_generation=3)

    # then
    assert (store.generations(), files.deleted) == ((1, 2), [wrap_path(3)])
