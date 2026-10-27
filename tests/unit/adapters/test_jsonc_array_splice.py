# SPDX-License-Identifier: AGPL-3.0-or-later
"""Element-level JSONC rendering: a changed array keeps the comments of the elements it did not touch."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ContentParseError
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.tree import tree_from_value

ADAPTER = JsoncAdapter()


RULE = ArrayRule.create(identity=("key", "command", "when"), required=("key", "command"), group=("key", "when"))


def _render(template: str, value: object) -> str:
    """Render `value` (a keybindings-shaped array, as plain JSON) into `template`, pairing by the binding rule."""
    return ADAPTER.render(tree_from_value(value), template, {(): RULE})


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


def test_removing_a_binding_that_carries_a_same_line_comment_is_refused() -> None:
    # given — the `// drop me` note is on the removed binding's own line
    template = (
        '[\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }, // drop me\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when & then — a comment is never deleted silently; the merge becomes a conflict
    with pytest.raises(ContentParseError):
        _render(template, [_a("x"), _c("z")])


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
    # given — a binding changed in place (two of its three fields kept), with a comment inside it
    template = '[\n    { "key": "a", /* why */ "command": "x", "args": 1 },\n    { "key": "b", "command": "y" }\n]'

    # when & then — the comment would be lost with the span, so the render refuses; a conflict, not a loss
    with pytest.raises(ContentParseError):
        _render(template, [{"key": "a", "command": "x", "args": 2}, _b("y")])


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
    rendered = ADAPTER.render(tree_from_value(target), template)

    # then
    assert ADAPTER.parse(rendered) == tree_from_value(target)


def test_a_comment_above_a_removed_element_stays_where_it_is() -> None:
    # given — `// why x` sits above x; the merge removes x and puts a different binding y in its place
    template = '[\n    // why x\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'

    # when
    rendered = _render(template, [_b("y"), _c("z")])

    # then — the note is not deleted: it stays above the line that follows, rather than being guessed onto y
    assert rendered == '[\n    // why x\n    {"key": "b", "command": "y"},\n    { "key": "c", "command": "z" }\n]'


def test_an_inline_comment_on_a_removed_element_refuses_rather_than_moving_onto_its_replacement() -> None:
    # given
    template = '[\n    { "key": "a", "command": "x" }, // inline why\n    { "key": "c", "command": "z" }\n]'

    # when & then
    with pytest.raises(ContentParseError):
        _render(template, [_b("y"), _c("z")])


def test_a_changed_binding_keeps_its_inline_comment() -> None:
    # given — the same binding (same key and command), with one field added: a change, not a replacement
    template = '[\n    { "key": "a", "command": "x" }, // keep me\n    { "key": "c", "command": "z" }\n]'

    # when
    rendered = _render(template, [{"key": "a", "command": "x", "args": 1}, _c("z")])

    # then
    assert "// keep me" in rendered


def test_a_new_multiline_element_takes_the_indent_step_of_the_arrays_own_elements() -> None:
    # given — a four-space file of multi-line bindings; the root is an array, so it has no members to read a step from
    template = '[\n    {\n        "key": "a",\n        "command": "x"\n    }\n]'

    # when
    rendered = _render(template, [_a("x"), _c("z")])

    # then — the added binding's members sit eight spaces in, as the existing ones do
    assert '\n        "key": "c",\n        "command": "z"\n    }' in rendered


def test_a_binding_added_to_a_file_that_mixes_one_line_and_multi_line_bindings_renders_valid_json() -> None:
    # given — a one-line binding, then a multi-line one; the one-line binding's `{ ` is no indent step
    template = '[\n  { "key": "a", "command": "x" },\n  {\n    "key": "b",\n    "command": "y"\n  }\n]'

    # when
    rendered = _render(template, [_a("x"), {"key": "b", "command": "y"}, _c("z")])

    # then — it parses back to the merged tree, so the merge is not a whole-file conflict
    assert ADAPTER.parse(rendered) == tree_from_value([_a("x"), {"key": "b", "command": "y"}, _c("z")])
    assert "{ " not in rendered.split("\n")[-3]


def test_a_two_field_binding_whose_command_changes_keeps_a_comment_inside_it_as_a_refused_rewrite() -> None:
    # given — the only binding of its group on each side, with a comment inside it; its command changes
    template = '[\n    { "key": "a", /* why */ "command": "x" },\n    { "key": "b", "command": "y" }\n]'

    # when & then — the same group pairs them, the span holds the comment, so the rewrite is refused, not silent
    with pytest.raises(ContentParseError):
        _render(template, [_a("x2"), _b("y")])


def test_a_group_with_several_bindings_on_each_side_pairs_them_in_order() -> None:
    # given — two bindings under one key on each side; both commands change, and a note sits above the first
    template = '[\n    // note on the first\n    { "key": "a", "command": "x" },\n    { "key": "a", "command": "y" }\n]'

    # when
    rendered = _render(template, [{"key": "a", "command": "x2"}, {"key": "a", "command": "y2"}])

    # then — each binding is rewritten where it stood, and the note stays on the first
    assert rendered == (
        '[\n    // note on the first\n    {"key": "a", "command": "x2"},\n    {"key": "a", "command": "y2"}\n]'
    )


def test_the_rendered_bytes_do_not_depend_on_the_hash_seed() -> None:
    # given — several groups change in one array, so the order groups are visited in could show
    script = (
        "from syncintosh.adapters.jsonc import JsoncAdapter\n"
        "from syncintosh.core.keyed_array import ArrayRule\n"
        "from syncintosh.core.tree import tree_from_value\n"
        "rule = ArrayRule.create(identity=('key','command','when'), required=('key','command'), group=('key','when'))\n"
        'template = \'[\\n  {"key": "a", "command": "x"},\\n  {"key": "b", "command": "y"},\\n\''
        ' \'  {"key": "c", "command": "z"},\\n  {"key": "d", "command": "w"}\\n]\'\n'
        "value = [{'key': 'd', 'command': 'w2'}, {'key': 'a', 'command': 'x2'}, {'key': 'c', 'command': 'z2'},"
        " {'key': 'b', 'command': 'y2'}]\n"
        "print(JsoncAdapter().render(tree_from_value(value), template, {(): rule}))\n"
    )
    outputs = {
        seed: subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(8)
    }

    # when & then — the same input renders to the same bytes under every seed
    assert len(set(outputs.values())) == 1


# --- property: on the identity path, a render parses back exactly or refuses, and no note is ever dropped ----------

_POOL = [("a", "x"), ("a", "y"), ("b", "x"), ("c", "z"), ("d", "w"), ("e", "v")]


@st.composite
def _binding_case(draw: st.DrawFn) -> tuple[str, list[tuple[str, str]], list[dict[str, object]]]:
    """A keybindings-shaped template, each binding under its own note, and a random merged target over the same pool."""
    base = draw(st.lists(st.sampled_from(_POOL), unique=True, min_size=1, max_size=5))
    target = draw(
        st.lists(
            st.tuples(st.sampled_from(_POOL), st.sampled_from([None, 1])),
            unique_by=lambda item: item[0],
            max_size=5,
        )
    )
    lines = ["["]
    for index, (key, command) in enumerate(base):
        lines.append(f"    // note {index}")
        comma = "," if index < len(base) - 1 else ""
        lines.append(f'    {{"key": "{key}", "command": "{command}"}}{comma}')
    lines.append("]")
    value: list[dict[str, object]] = []
    for (key, command), args in target:
        binding: dict[str, object] = {"key": key, "command": command}
        if args is not None:
            binding["args"] = args
        value.append(binding)
    return "\n".join(lines), base, value


def _try_render(template: str, value: list[dict[str, object]]) -> str | None:
    """The render, or `None` when the adapter refuses it (a conflict, which is allowed; a silent wrong file is not)."""
    try:
        return _render(template, value)
    except ContentParseError:
        return None


@given(case=_binding_case())
def test_a_keyed_render_parses_back_exactly_or_refuses(case: tuple[str, list[tuple[str, str]], list[dict]]) -> None:
    # given
    template, _, value = case

    # when
    rendered = _try_render(template, value)

    # then
    assert rendered is None or ADAPTER.parse(rendered) == tree_from_value(value)


@given(case=_binding_case())
def test_a_keyed_render_never_drops_a_note_above_a_binding(case: tuple[str, list[tuple[str, str]], list[dict]]) -> None:
    # given
    template, base, value = case

    # when
    rendered = _try_render(template, value)

    # then — a note stays with the array, whichever binding it sat above; nothing is removed silently
    missing = [index for index in range(len(base)) if rendered is not None and f"// note {index}" not in rendered]
    assert missing == []
