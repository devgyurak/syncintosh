# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for `JsoncAdapter.render_edits`: the settled edits `render` applies (issue #27)."""

from __future__ import annotations

from itertools import pairwise

import pytest

from syncintosh.adapters import jsonc
from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.tree import tree_from_value

ADAPTER = JsoncAdapter()
TEMPLATE = '{\n  "a": 1, // one\n  "b": {"x": 1, "y": 2},\n  "c": [1, 2]\n}\n'

CHANGES = {
    "nothing changed": {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2]},
    "a value changed": {"a": 9, "b": {"x": 1, "y": 2}, "c": [1, 2]},
    "a key added": {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2], "d": 4},
    "a key removed": {"a": 1, "c": [1, 2]},
    "a member of a one-line object removed": {"a": 1, "b": {"x": 1}, "c": [1, 2]},
    "an array element added": {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2, 3]},
    "several at once": {"b": {"x": 5}, "c": [2], "e": {"f": 1}},
}


@pytest.mark.parametrize("value", list(CHANGES.values()), ids=list(CHANGES))
def test_applying_the_edits_gives_the_rendered_text(value) -> None:
    # given
    tree = tree_from_value(value)

    # when
    edits = ADAPTER.render_edits(tree, TEMPLATE)

    # then
    assert jsonc._apply_edits(TEMPLATE, list(edits)) == ADAPTER.render(tree, TEMPLATE)


@pytest.mark.parametrize("value", list(CHANGES.values()), ids=list(CHANGES))
def test_the_edits_are_in_order_and_do_not_overlap(value) -> None:
    # given
    tree = tree_from_value(value)

    # when
    edits = ADAPTER.render_edits(tree, TEMPLATE)

    # then
    overlapping = [(a, b) for a, b in pairwise(edits) if b.start < a.end]
    assert overlapping == []


def test_a_tree_the_template_already_holds_has_no_edits() -> None:
    # when
    edits = ADAPTER.render_edits(ADAPTER.parse(TEMPLATE), TEMPLATE)

    # then
    assert edits == ()
