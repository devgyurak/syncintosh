# SPDX-License-Identifier: AGPL-3.0-or-later
"""Element-level JSONC rendering: a changed array keeps the comments of the elements it did not touch."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ContentParseError
from syncintosh.core.tree import tree_from_value

ADAPTER = JsoncAdapter()


def _render(template: str, value: object) -> str:
    """Render `value` (the merged array, as plain JSON) into `template`."""
    return ADAPTER.render(tree_from_value(value), template)


def _a(command: str) -> dict[str, object]:
    return {"key": "a", "command": command}


def _b(command: str) -> dict[str, object]:
    return {"key": "b", "command": command}


def _c(command: str) -> dict[str, object]:
    return {"key": "c", "command": command}


# --- each layout case: the exact text an element-level edit produces ---------------------------------------


def test_an_added_element_goes_after_the_kept_ones_and_their_comments_stay() -> None:
    # given
    template = (
        '[\n    // keep the first\n    { "key": "a", "command": "x" },\n'
        '    { "key": "b", "command": "y" } // trailing note\n]'
    )

    # when
    rendered = _render(template, [_a("x"), _b("y"), _c("z")])

    # then
    assert rendered == (
        '[\n    // keep the first\n    { "key": "a", "command": "x" },\n'
        '    { "key": "b", "command": "y" }, // trailing note\n'
        '    {"key": "c", "command": "z"}\n]'
    )


def test_a_removed_element_takes_its_line_and_its_same_line_comment() -> None:
    # given
    template = (
        '[\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }, // drop me\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_a("x"), _c("z")])

    # then
    assert rendered == '[\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'


def test_removing_the_last_element_drops_the_comma_its_predecessor_was_left_with() -> None:
    # given
    template = '[\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_a("x")])

    # then
    assert rendered == '[\n    { "key": "a", "command": "x" }\n]'


def test_a_changed_element_rewrites_only_its_own_span() -> None:
    # given
    template = '[\n    // note on a\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_a("x2"), _b("y")])

    # then
    assert rendered == '[\n    // note on a\n    {"key": "a", "command": "x2"},\n    { "key": "b", "command": "y" }\n]'


def test_an_added_first_element_goes_above_the_first_elements_own_comment() -> None:
    # given
    template = '[\n    // note on a\n    { "key": "a", "command": "x" }\n]'

    # when
    rendered = _render(template, [_c("z"), _a("x")])

    # then
    assert rendered == '[\n    {"key": "c", "command": "z"},\n    // note on a\n    { "key": "a", "command": "x" }\n]'


def test_appending_to_an_array_with_a_trailing_comma_keeps_that_style() -> None:
    # given
    template = '[\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" },\n]'

    # when
    rendered = _render(template, [_a("x"), _b("y"), _c("z")])

    # then
    assert rendered == (
        '[\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" },\n'
        '    {"key": "c", "command": "z"},\n]'
    )


def test_a_comment_inside_a_changed_element_refuses_the_rewrite_instead_of_dropping_it() -> None:
    # given
    template = '[\n    { "key": "a", /* why */ "command": "x" },\n    { "key": "b", "command": "y" }\n]'

    # when & then — the comment would be lost with the span, so the render refuses; a conflict, not a loss
    with pytest.raises(ContentParseError):
        _render(template, [_a("x2"), _b("y")])


def test_an_inline_array_is_rewritten_whole_as_before() -> None:
    # given
    template = '{\n    "list": [1, 2]\n}'

    # when
    rendered = _render(template, {"list": [1, 3]})

    # then
    assert ADAPTER.parse(rendered) == tree_from_value({"list": [1, 3]})


def test_an_empty_array_that_gains_elements_is_rewritten_whole() -> None:
    # given
    template = "[\n]"

    # when
    rendered = _render(template, [_a("x")])

    # then
    assert ADAPTER.parse(rendered) == tree_from_value([_a("x")])


# --- property: whatever elements are kept, dropped, changed, or added, the render parses back exactly -------

_ints = st.integers(min_value=0, max_value=9)


@st.composite
def _template_and_target(draw: st.DrawFn) -> tuple[str, list[int]]:
    """A clean multi-line array, some elements under a comment line, and the list the merge wants."""
    values = draw(st.lists(_ints, min_size=1, max_size=6))
    commented = draw(st.lists(st.booleans(), min_size=len(values), max_size=len(values)))
    trailing = draw(st.booleans())
    lines = ["["]
    for index, (value, has_comment) in enumerate(zip(values, commented, strict=True)):
        if has_comment:
            lines.append(f"    // note {index}")
        comma = "," if index < len(values) - 1 or trailing else ""
        lines.append(f"    {value}{comma}")
    lines.append("]")
    target = draw(st.lists(_ints, max_size=6))
    return "\n".join(lines), target


@given(case=_template_and_target())
def test_an_element_level_render_always_parses_back_to_the_merged_tree(case: tuple[str, list[int]]) -> None:
    # given
    template, target = case

    # when
    rendered = _render(template, target)

    # then
    assert ADAPTER.parse(rendered) == tree_from_value(target)
