# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for the append-only merge rule (issue #56, decision D2)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.core.append import EXTENDED, FORKED, SAME, append_merge

BYTES = st.binary(max_size=64)


@pytest.mark.parametrize(
    ("local", "remote", "kind", "data", "longer"),
    [
        (b"a\nb\n", b"a\nb\n", SAME, b"a\nb\n", None),
        (b"", b"", SAME, b"", None),
        (b"a\n", b"a\nb\n", EXTENDED, b"a\nb\n", "remote"),
        (b"a\nb\n", b"a\n", EXTENDED, b"a\nb\n", "local"),
        (b"", b"a\n", EXTENDED, b"a\n", "remote"),
        (b"a\n", b"", EXTENDED, b"a\n", "local"),
        # Diverged after a shared prefix: the repository's copy is the one kept.
        (b"a\nx\n", b"a\ny\n", FORKED, b"a\ny\n", None),
        (b"x", b"y", FORKED, b"y", None),
    ],
)
def test_two_copies_are_the_same_one_extending_the_other_or_forked(
    local: bytes, remote: bytes, kind: str, data: bytes, longer: str | None
) -> None:
    # when
    merged = append_merge(local, remote)

    # then
    assert (merged.kind, merged.data, merged.longer, merged.forked) == (kind, data, longer, kind == FORKED)


@given(BYTES)
@settings(deadline=None)
def test_merging_a_copy_with_itself_changes_nothing(data: bytes) -> None:
    # when
    merged = append_merge(data, data)

    # then
    assert (merged.kind, merged.data) == (SAME, data)


@given(BYTES, BYTES)
@settings(deadline=None)
def test_an_extension_always_wins_whichever_side_it_is_on(prefix: bytes, tail: bytes) -> None:
    # given
    longer = prefix + tail

    # when
    forward, backward = append_merge(prefix, longer), append_merge(longer, prefix)

    # then
    assert (forward.data, backward.data) == (longer, longer)


@given(BYTES, BYTES)
@settings(deadline=None)
def test_the_kept_bytes_do_not_depend_on_the_order_unless_forked(left: bytes, right: bytes) -> None:
    # when
    one, other = append_merge(left, right), append_merge(right, left)

    # then — a fork keeps the remote side by definition; everything else is symmetric
    assert one.forked == other.forked and (one.forked or one.data == other.data)


@given(BYTES, BYTES)
@settings(deadline=None)
def test_it_is_forked_exactly_when_neither_side_is_a_prefix_of_the_other(left: bytes, right: bytes) -> None:
    # when
    merged = append_merge(left, right)

    # then
    assert merged.forked == (not left.startswith(right) and not right.startswith(left))


@given(BYTES, BYTES)
@settings(deadline=None)
def test_merging_the_result_again_is_a_no_op(left: bytes, right: bytes) -> None:
    # given
    once = append_merge(left, right)

    # when
    twice = append_merge(once.data, right)

    # then — a fork keeps the remote's copy, which is then the same as the remote
    assert twice.data == once.data


@given(BYTES, BYTES)
@settings(deadline=None)
def test_the_result_never_loses_bytes_the_longer_side_had(left: bytes, right: bytes) -> None:
    # when
    merged = append_merge(left, right)

    # then
    assert merged.forked or len(merged.data) == max(len(left), len(right))
