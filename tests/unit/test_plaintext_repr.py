# SPDX-License-Identifier: AGPL-3.0-or-later
"""A repr never shows a setting's value or a file's bytes; a traceback or a log line must not leak them."""

from __future__ import annotations

import pytest

from syncintosh.core.content_merge import ContentMerge
from syncintosh.core.keyed_array import GroupConflict, KeyedMerge
from syncintosh.core.models import Conflict, TextMerge, TreeMerge
from syncintosh.core.tree import ABSENT, Array, EmptyObject, Object, Scalar, Tree
from syncintosh.services.diff import FileDiff
from syncintosh.services.merge_service import RenderedMerge
from syncintosh.services.sync import _Collect, _Projected, _Settled

SECRET = "SECRET-PLAINTEXT-XYZ"


def _leaves() -> list[object]:
    scalar = Scalar("str", SECRET)
    array = Array((scalar,))
    obj = Object(((SECRET, scalar),))
    return [
        scalar,
        array,
        obj,
        Tree.create({("k",): scalar, ("arr",): array, (): EmptyObject()}),
        TreeMerge(merged=Tree.create({("k",): scalar}), conflicts=()),
        TextMerge(text=SECRET, conflicted=False),
        ContentMerge(data=SECRET.encode(), conflicted=False),
        FileDiff(path="p", status="changed", here=SECRET, repo=SECRET, addresses=((("k",), None),)),
        GroupConflict(element=(("key", "k"), ("when", None)), base=array, local=array, remote=array),
        KeyedMerge(merged=array, conflicts=()),
        _Collect(contents={"f": SECRET.encode()}, views={"f": SECRET.encode()}, seen={"f": SECRET.encode()}),
        _Projected(raw=SECRET.encode(), view=SECRET.encode(), failure=None),
        RenderedMerge(text=SECRET, conflicts=(), conflicted=False),
        _Settled(data=SECRET.encode(), detail="d", views=()),
        Conflict(
            path=("k",),
            base=scalar,
            local=scalar,
            remote=scalar,
            element=(("key", "ctrl+k"), ("when", f"resourceHost == '{SECRET}'")),
        ),
    ]


@pytest.mark.parametrize("value", _leaves(), ids=lambda value: type(value).__name__)
def test_a_repr_never_shows_the_plaintext(value) -> None:
    # when
    text = repr(value)

    # then
    assert SECRET not in text


def test_an_absent_leaf_still_prints_as_absent() -> None:
    # when & then
    assert repr(ABSENT) == "ABSENT"
