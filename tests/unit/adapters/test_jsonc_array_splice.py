# SPDX-License-Identifier: AGPL-3.0-or-later
"""Element-level JSONC rendering: a changed array keeps the comments of the elements it did not touch."""

from __future__ import annotations

import itertools
import os
import random
import re
import subprocess
import sys
import time
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from syncintosh.adapters import jsonc
from syncintosh.adapters.jsonc import (
    CHANGED_WITH_NOTE_REFUSED,
    UNRULED_REMOVAL_REFUSED,
    VALUE_WITH_NOTE_REFUSED,
    JsoncAdapter,
)
from syncintosh.core.errors import ContentParseError, RenderRefused
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


def _y(command: str) -> dict[str, object]:
    return {"key": "y", "command": command}


def _z(command: str) -> dict[str, object]:
    return {"key": "z", "command": command}


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


def test_removing_a_binding_takes_its_same_line_comment_with_it() -> None:
    # given — the `// drop me` note is on the removed binding's own line
    template = (
        '[\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }, // drop me\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_a("x"), _c("z")])

    # then — the binding and its own note go; the others keep theirs
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

    # when — a rule-less array refuses a removal that would take a note, so the render may refuse; never a wrong file
    rendered = _try_plain_render(template, target)

    # then
    assert rendered is None or ADAPTER.parse(rendered) == tree_from_value(target)


def test_an_inline_comment_on_a_removed_element_goes_with_it_and_never_moves_onto_its_replacement() -> None:
    # given
    template = '[\n    { "key": "a", "command": "x" }, // inline why\n    { "key": "c", "command": "z" }\n]'

    # when
    rendered = _render(template, [_b("y"), _c("z")])

    # then
    assert "inline why" not in rendered
    assert ADAPTER.parse(rendered) == tree_from_value([_b("y"), _c("z")])


def test_a_moved_binding_keeps_its_own_comments_and_its_text() -> None:
    # given — the remote moved `b` ahead of `a`; both carry a note, and `b` has an inline note too
    template = (
        '[\n    // note on a\n    { "key": "a", "command": "x" },\n'
        '    // note on b\n    { "key": "b", "command": "y" }, // b inline\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_b("y"), _a("x"), _c("z")])

    # then — it parses to the moved order, and every note is still there
    assert ADAPTER.parse(rendered) == tree_from_value([_b("y"), _a("x"), _c("z")])
    assert all(note in rendered for note in ("// note on a", "// note on b", "// b inline"))


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
    # given — several groups change in one array, with notes and a reorder, visited in a hash-dependent order
    script = (
        "from syncintosh.adapters.jsonc import JsoncAdapter\n"
        "from syncintosh.core.keyed_array import ArrayRule\n"
        "from syncintosh.core.tree import tree_from_value\n"
        "rule = ArrayRule.create(identity=('key','command','when'), required=('key','command'), group=('key','when'))\n"
        'template = (\'[\\n  // n-a\\n  {"key": "a", "command": "x"}, // ia\\n\'\n'
        '  \'  // n-b\\n  {"key": "b", "command": "y"},\\n\'\n'
        '  \'  // n-c\\n  {"key": "c", "command": "z"},\\n\'\n'
        '  \'  {"key": "d", "command": "w"}\\n]\')\n'
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


_POOL = [("a", "x"), ("a", "y"), ("b", "x"), ("c", "z"), ("d", "w"), ("e", "v")]


def _try_plain_render(template: str, value: list[int]) -> str | None:
    """The render of an array with no rule, or `None` when the adapter refuses it."""
    try:
        return ADAPTER.render(tree_from_value(value), template)
    except ContentParseError:
        return None


def _try_render(template: str, value: list[dict[str, object]]) -> str | None:
    """The render, or `None` when the adapter refuses it (a conflict, which is allowed; a silent wrong file is not)."""
    try:
        return _render(template, value)
    except ContentParseError:
        return None


@st.composite
def _notes_case(draw: st.DrawFn) -> tuple[str, list[tuple[str, str]], list[dict[str, object]]]:
    """A keybindings template with notes above, beside and inside each binding, section titles, and a random target.

    A section title is a comment separated from the binding below it by a blank line, so it belongs to no binding.
    """
    base = draw(st.lists(st.sampled_from(_POOL), unique=True, min_size=1, max_size=5))
    with_inline = draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base)))
    with_section = draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base)))
    lines = ["["]
    for index, (key, command) in enumerate(base):
        if with_section[index]:
            lines.extend(["", f"    // section {index}", ""])
        lines.append(f"    // note {index}")
        comma = "," if index < len(base) - 1 else ""
        inline = f" // inl {index}" if with_inline[index] else ""
        lines.append(f'    {{"key": "{key}", /* in {index} */ "command": "{command}"}}{comma}{inline}')
    lines.append("]")
    chosen = draw(st.lists(st.sampled_from(base), unique=True, max_size=5))
    order = draw(st.permutations(chosen)) if chosen else []
    changed = draw(st.lists(st.booleans(), min_size=len(order), max_size=len(order)))
    value: list[dict[str, object]] = []
    for (key, command), change in zip(order, changed, strict=True):
        binding: dict[str, object] = {"key": key, "command": command}
        if change:
            binding["args"] = 1
        value.append(binding)
    return "\n".join(lines), base, value


@given(case=_notes_case())
def test_a_keyed_render_parses_back_exactly_or_refuses(case: tuple[str, list[tuple[str, str]], list[dict]]) -> None:
    # given
    template, _, value = case

    # when
    rendered = _try_render(template, value)

    # then
    assert rendered is None or ADAPTER.parse(rendered) == tree_from_value(value)


def _inner_notes_lost(index: int, rendered: str, template: str) -> bool:
    """True when a binding's inner note, or its inline note, is in the template but gone from the render."""
    inline_present = f"// inl {index}" in template
    return f"/* in {index} */" not in rendered or (inline_present and f"// inl {index}" not in rendered)


@given(case=_notes_case())
def test_a_kept_binding_keeps_its_notes_a_removed_one_loses_them_and_section_titles_stay(
    case: tuple[str, list[tuple[str, str]], list[dict]],
) -> None:
    # given
    template, base, value = case

    # when
    rendered = _try_render(template, value)

    # then — checked as text, and only for a render that happened: a kept binding's note sits directly above its own
    # line, a removed binding's notes are gone, a section title stays, and no two blank lines sit side by side
    text = "" if rendered is None else rendered
    kept = {f"{binding['key']}/{binding['command']}" for binding in value}
    removed_notes = [
        index
        for index, (key, command) in enumerate(base)
        if rendered is not None and f"{key}/{command}" not in kept and f"// note {index}" in text
    ]
    not_directly_above = [
        index
        for index, (key, command) in enumerate(base)
        if rendered is not None and f"{key}/{command}" in kept and f'// note {index}\n    {{"key": "{key}"' not in text
    ]
    sections_lost = [
        index
        for index in range(len(base))
        if rendered is not None and f"// section {index}" in template and f"// section {index}" not in text
    ]
    doubled = rendered is not None and _has_doubled_blank(text)
    assert removed_notes == []
    assert not_directly_above == []
    assert sections_lost == []
    assert doubled is False


def test_moved_bindings_take_their_own_notes_to_their_new_place() -> None:
    # given — each binding has a note directly above it; the merged order is b, a, c
    template = (
        '[\n    // note on a\n    { "key": "a", "command": "x" },\n'
        '    // note on b\n    { "key": "b", "command": "y" },\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_b("y"), _a("x"), _c("z")])

    # then — each note sits directly above its own binding, and no blank line is left behind
    assert rendered == (
        '[\n    // note on b\n    { "key": "b", "command": "y" },\n'
        '    // note on a\n    { "key": "a", "command": "x" },\n'
        '    { "key": "c", "command": "z" }\n]'
    )


def test_a_rule_less_array_refuses_a_removal_that_would_drop_a_note() -> None:
    # given — the `editor.rulers` array has no rule, so nothing says which value a note belongs to
    template = '{\n  "editor.rulers": [\n    80, // pep8\n    120\n  ]\n}'

    # when & then — the removal of 80 would take its note, so the render refuses rather than dropping it
    with pytest.raises(ContentParseError):
        ADAPTER.render(tree_from_value({"editor.rulers": [79, 120]}), template)


def test_a_comma_alone_on_its_line_is_not_spliced_and_stays_valid_json() -> None:
    # given — a comma starts its own line between two bindings
    template = '[\n  { "key": "a", "command": "x" }\n  ,\n  { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_b("y"), _a("x")])

    # then — the array is rewritten whole, so the result parses to the merged order
    assert ADAPTER.parse(rendered) == tree_from_value([_b("y"), _a("x")])


def _has_doubled_blank(text: str) -> bool:
    """True when two blank lines sit side by side, a whitespace-only difference every sync would show."""
    return re.search(r"\r?\n[ \t]*\r?\n[ \t]*\r?\n", text) is not None


def test_a_block_comment_note_moves_with_its_binding_and_stays_directly_above_it() -> None:
    # given — a note written as a block comment sits directly above `a`, a line note above `b`
    template = (
        '[\n    /* note on a */\n    { "key": "a", "command": "x" },\n'
        '    // note on b\n    { "key": "b", "command": "y" },\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when — the merged order is b, a, c
    rendered = _render(template, [_b("y"), _a("x"), _c("z")])

    # then — each note, block or line, sits directly above its own binding
    assert rendered == (
        '[\n    // note on b\n    { "key": "b", "command": "y" },\n'
        '    /* note on a */\n    { "key": "a", "command": "x" },\n'
        '    { "key": "c", "command": "z" }\n]'
    )


def test_a_block_comment_note_refuses_a_rule_less_removal_that_would_take_it() -> None:
    # given — the `editor.rulers` array has no rule, and a block note sits above the 80 that is removed
    template = '{\n  "editor.rulers": [\n    /* why 80 */\n    80,\n    120\n  ]\n}'

    # when & then — the refusal carries the adapter's fixed reason, not a value
    with pytest.raises(RenderRefused) as refused:
        ADAPTER.render(tree_from_value({"editor.rulers": [120]}), template)
    assert refused.value.reason == UNRULED_REMOVAL_REFUSED


def test_a_removal_between_two_blank_lines_leaves_exactly_one_blank_line() -> None:
    # given — bindings separated by blank lines
    template = (
        '[\n    { "key": "a", "command": "x" },\n\n'
        '    { "key": "b", "command": "y" },\n\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when — `b` is removed
    rendered = _render(template, [_a("x"), _c("z")])

    # then — one blank line is left between `a` and `c`, not two
    assert rendered == '[\n    { "key": "a", "command": "x" },\n\n    { "key": "c", "command": "z" }\n]'


def test_two_adjacent_removals_between_blank_lines_leave_one_blank_line_not_two() -> None:
    # given — `b` and `c` sit together between two blank lines; each alone would see one blank line beside it
    template = (
        '[\n    { "key": "a", "command": "x" },\n\n'
        '    { "key": "b", "command": "y" },\n    { "key": "c", "command": "z" },\n\n'
        '    { "key": "d", "command": "w" }\n]'
    )

    # when — both are removed
    rendered = _render(template, [_a("x"), {"key": "d", "command": "w"}])

    # then — exactly one blank line separates `a` from `d`
    assert rendered == '[\n    { "key": "a", "command": "x" },\n\n    { "key": "d", "command": "w" }\n]'


def test_a_moved_binding_between_blank_lines_leaves_no_doubled_blank_line() -> None:
    # given — bindings separated by blank lines
    template = (
        '[\n    { "key": "a", "command": "x" },\n\n'
        '    { "key": "b", "command": "y" },\n\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when — the merged order is b, a, c
    rendered = _render(template, [_b("y"), _a("x"), _c("z")])

    # then — it parses to the merged order, and no two blank lines sit side by side
    assert ADAPTER.parse(rendered) == tree_from_value([_b("y"), _a("x"), _c("z")])
    assert _has_doubled_blank(rendered) is False


def test_a_changed_binding_with_a_comment_inside_is_refused_with_its_fixed_reason() -> None:
    # given — `a` has a comment inside it and gains an `args`, so the same binding is rewritten in its span
    template = '[\n    { "key": "a", /* why */ "command": "x" },\n    { "key": "c", "command": "z" }\n]'

    # when & then — the refusal names the cause with the adapter's fixed text, and no value
    with pytest.raises(RenderRefused) as refused:
        _render(template, [{"key": "a", "command": "x", "args": 1}, _c("z")])
    assert refused.value.reason == CHANGED_WITH_NOTE_REFUSED
    assert "x" not in CHANGED_WITH_NOTE_REFUSED.split()


def test_a_line_note_above_a_removed_binding_goes_with_it() -> None:
    # given — `// why x` documents x, and the merge removes x
    template = '[\n    // why x\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'

    # when
    rendered = _render(template, [_c("z")])

    # then — the note belongs to the removed binding, so it goes too
    assert rendered == '[\n    { "key": "c", "command": "z" }\n]'


def test_a_block_note_above_a_removed_binding_goes_with_it() -> None:
    # given — a one-line block note documents x, and x is removed
    template = '[\n    /* why x */\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'

    # when
    rendered = _render(template, [_c("z")])

    # then
    assert rendered == '[\n    { "key": "c", "command": "z" }\n]'


def test_a_section_title_separated_by_a_blank_line_stays_when_the_binding_below_it_is_removed() -> None:
    # given — the title belongs to no binding, because a blank line separates it from `a`
    template = (
        '[\n    // section\n\n    // note a\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_c("z")])

    # then — the note goes with `a`; the section title stays
    assert rendered == '[\n    // section\n\n    { "key": "c", "command": "z" }\n]'


def test_a_changed_binding_without_a_pair_takes_its_notes_with_it() -> None:
    # given — `a` gains a `when`, so it is no longer the same binding: its notes go with the old one
    template = (
        '[\n    // upper note\n    { "key": "a", "command": "x" }, // inline note\n'
        '    { "key": "c", "command": "z" }\n]'
    )
    gained_when = {"key": "a", "command": "x", "when": "editorFocus"}

    # when
    rendered = _render(template, [gained_when, _c("z")])

    # then — no note of the old binding is left, and the new one is in place
    assert "upper note" not in rendered
    assert "inline note" not in rendered
    assert ADAPTER.parse(rendered) == tree_from_value([gained_when, _c("z")])


def test_a_removed_bindings_note_does_not_follow_a_binding_that_moves() -> None:
    # given — a is removed with its note; b keeps its note and moves
    template = (
        '[\n    // note on a\n    { "key": "a", "command": "x" },\n'
        '    // note on b\n    { "key": "b", "command": "y" },\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when — the merged order is c, b
    rendered = _render(template, [_c("z"), _b("y")])

    # then — a's note is gone; b's note is still directly above b
    assert rendered == (
        '[\n    { "key": "c", "command": "z" },\n    // note on b\n    { "key": "b", "command": "y" }\n]'
    )


def test_a_blank_line_is_not_left_next_to_the_opening_bracket() -> None:
    # given — `a` is first, with a blank line between it and `b`
    template = '[\n    { "key": "a", "command": "x" },\n\n    { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_b("y")])

    # then — the blank line after the removed first binding does not survive as the first line of the array
    assert rendered == '[\n    { "key": "b", "command": "y" }\n]'


def test_a_blank_line_is_not_left_before_the_closing_bracket() -> None:
    # given — `b` is last, with a blank line between it and `a`
    template = '[\n    { "key": "a", "command": "x" },\n\n    { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_a("x")])

    # then — no blank line before `]`, and `a` loses the comma it no longer needs
    assert rendered == '[\n    { "key": "a", "command": "x" }\n]'


def test_two_blank_lines_the_user_put_between_untouched_bindings_both_stay() -> None:
    # given — two blank lines between `a` and `b`, and one blank line before the removed `c`
    template = (
        '[\n    { "key": "a", "command": "x" },\n\n\n    { "key": "b", "command": "y" },\n\n'
        '    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_a("x"), _b("y")])

    # then — the two blank lines between a and b are untouched; the blank line before `]` is not
    assert rendered == '[\n    { "key": "a", "command": "x" },\n\n\n    { "key": "b", "command": "y" }\n]'


def test_a_crlf_removal_next_to_a_blank_line_leaves_one_blank_line_and_keeps_crlf() -> None:
    # given — CRLF line endings, a blank line between `a` and `b`, and `a` is removed
    template = '[\r\n    { "key": "a", "command": "x" },\r\n\r\n    { "key": "b", "command": "y" }\r\n]'

    # when
    rendered = _render(template, [_b("y")])

    # then
    assert rendered == '[\r\n    { "key": "b", "command": "y" }\r\n]'


def test_a_two_thousand_line_note_block_moves_within_a_generous_time_limit() -> None:
    # given — a binding under two thousand note lines, and the merge moves it below its neighbour
    notes = "".join(f"    // note line {index}\n" for index in range(2000))
    template = "[\n" + notes + '    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }\n]\n'
    started = time.process_time()

    # when
    rendered = _render(template, [_b("y"), _a("x")])

    # then — the note block still sits directly above `a`; the bound is far above the expected time, so CI stays stable
    assert rendered.index('{ "key": "b"') < rendered.index("// note line 0") < rendered.index('{ "key": "a"')
    assert rendered.index("// note line 1999") < rendered.index('{ "key": "a"')
    assert rendered.count("// note line") == 2000
    assert time.process_time() - started < 2.0


def test_a_section_title_stays_apart_from_a_binding_inserted_where_the_last_removed_one_was() -> None:
    # given — a section title, a blank line, then the only binding, which is removed for a new one
    template = '[\n    // Section\n\n    { "key": "a", "command": "x" }\n]'

    # when
    rendered = _render(template, [_b("y")])

    # then — the blank line stays, so the title is not glued to the new binding as its note
    assert rendered == '[\n    // Section\n\n    {"key": "b", "command": "y"}\n]'


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_a_binding_inserted_above_a_section_title_keeps_the_blank_line_in_both_line_endings(newline: str) -> None:
    # given — the same layout as the LF case, written with CRLF line endings
    template = (
        '[\n    {"key": "c", "command": "y"},\n    /* N1 */\n    {"key": "b", "command": "z"},\n'
        '    {"key": "c", "command": "x"},\n\n    // S4\n\n    {"key": "d", "command": "z"}\n]'
    ).replace("\n", newline)

    # when
    rendered = _render(template, [{"key": "a", "command": "z"}])

    # then
    assert rendered == f'[{newline}    {{"key": "a", "command": "z"}}{newline}{newline}    // S4{newline}]'


def test_a_binding_inserted_after_a_multi_line_block_comment_on_the_opening_bracket_keeps_the_blank_line() -> None:
    # given — the opening bracket carries a block comment with a blank line inside it; `x` is removed, `z` is added
    template = '[ /* a\n\n   b */\n    {"key": "x", "command": "1"},\n\n    // S\n\n    {"key": "y", "command": "2"}\n]'

    # when
    rendered = _render(template, [{"key": "z", "command": "9"}])

    # then — the blank line inside the comment is not a blank line of the array, and the one before `// S` stays
    assert rendered == '[ /* a\n\n   b */\n    {"key": "z", "command": "9"}\n\n    // S\n]'


def test_a_removal_right_before_the_closing_bracket_leaves_no_blank_line_before_it() -> None:
    # given — the last binding is removed, and a blank line the user had before it would end up against the bracket
    template = '[\n    {"key": "a", "command": "1"},\n\n    {"key": "x", "command": "2"}\n\n]'

    # when
    rendered = _render(template, [{"key": "a", "command": "1"}])

    # then
    assert rendered == '[\n    {"key": "a", "command": "1"}\n]'


def test_replacing_the_last_binding_leaves_no_blank_line_before_the_closing_bracket() -> None:
    # given — a blank line sits between the last binding and the closing bracket
    template = '[\n    {"key": "x", "command": "c"}\n\n]'

    # when — `x` is replaced by `z`, which is inserted where `x` was
    rendered = _render(template, [{"key": "z", "command": "c"}])

    # then — the blank line the edit touches goes, so the bracket follows the new binding
    assert rendered == '[\n    {"key": "z", "command": "c"}\n]'


def test_a_binding_replacing_the_last_one_after_a_kept_binding_leaves_no_blank_line_before_the_bracket() -> None:
    # given — `x` is the last binding, with a blank line before the bracket, and `a` is kept above it
    template = '[\n    {"key": "a", "command": "c"},\n    {"key": "x", "command": "c"}\n\n]'

    # when — `x` is replaced by `z`
    rendered = _render(template, [{"key": "a", "command": "c"}, {"key": "z", "command": "c"}])

    # then
    assert rendered == '[\n    {"key": "a", "command": "c"},\n    {"key": "z", "command": "c"}\n]'


def test_removing_two_bindings_above_a_blank_line_and_the_bracket_leaves_no_blank_line_before_it() -> None:
    # given — `x` and `y` are removed; the blank line between them and the one before the bracket both go with them
    template = (
        '[\n    {"key": "a", "command": "c"},\n    {"key": "x", "command": "c"},\n\n'
        '    {"key": "y", "command": "c"}\n\n]'
    )

    # when
    rendered = _render(template, [{"key": "a", "command": "c"}])

    # then
    assert rendered == '[\n    {"key": "a", "command": "c"}\n]'


def test_removing_three_bindings_in_single_blank_line_steps_leaves_no_blank_line_before_the_bracket() -> None:
    # given — `x`, `y` and `w` are each one blank line apart, and a blank line sits before the bracket;
    # only `a` is kept
    template = (
        '[\n    {"key": "a", "command": "c"},\n    {"key": "x", "command": "c"},\n\n'
        '    {"key": "y", "command": "c"},\n\n    {"key": "w", "command": "c"}\n\n]'
    )

    # when
    rendered = _render(template, [{"key": "a", "command": "c"}])

    # then — the group goes as one piece, and no blank line is left in front of the bracket
    assert rendered == '[\n    {"key": "a", "command": "c"}\n]'


def test_the_blank_line_between_two_removed_bindings_goes_when_a_blank_line_stands_above_them() -> None:
    # given — a blank line sits above `k3`, and one more blank line sits between `k3` and `k1`; both are removed, and
    # `k2` is merged in where `k3` was
    template = '[\n    // S0\n\n    {"key": "k3", "command": "v"},\n\n    {"key": "k1", "command": "v"}\n    // S3\n]'

    # when
    rendered = _render(template, [{"key": "k2", "command": "w"}])

    # then — the blank line between the two removed bindings goes with the group, so `// S3` sits right under `k2`
    assert rendered == '[\n    // S0\n\n    {"key": "k2", "command": "w"}\n    // S3\n]'


def test_a_removed_group_at_the_opening_bracket_takes_the_blank_line_between_its_parts() -> None:
    # given — the first two bindings are removed, with one blank line between them; `new` is merged in
    template = '[\n    {"key": "k0", "command": "v"},\n\n    {"key": "k1", "command": "v"}\n    // S2\n]'

    # when
    rendered = _render(template, [{"key": "new", "command": "w"}])

    # then — the group sits against the opening bracket, so its inner blank line goes
    assert rendered == '[\n    {"key": "new", "command": "w"}\n    // S2\n]'


@pytest.mark.parametrize(
    ("ranges", "merged"),
    [
        ([(0, 5), (7, 9)], [(0, 5), (7, 9)]),
        ([(0, 5), (5, 9)], [(0, 5), (5, 9)]),
        ([(0, 5), (3, 9), (12, 14)], [(0, 9), (12, 14)]),
        ([(7, 9), (0, 10), (2, 3)], [(0, 10)]),
    ],
    ids=["disjoint", "touching-stay-apart", "overlapping", "nested-and-unordered"],
)
def test_deletion_ranges_merge_only_where_they_overlap(
    ranges: list[tuple[int, int]], merged: list[tuple[int, int]]
) -> None:
    # when
    result = jsonc._union(ranges)

    # then — the ranges come out in order, and two that only touch are not joined
    assert result == merged


def test_an_insertion_where_two_deletions_meet_is_not_lost() -> None:
    # given — two deletions that meet at offset 5, and an insertion at that offset
    text = "0123456789"
    insertion = jsonc._Edit(5, 5, "X")
    deletions = [jsonc._Edit(start, end, "") for start, end in jsonc._union([(0, 5), (5, 9)])]

    # when
    result = jsonc._apply_edits(text, [insertion, *deletions])

    # then — the insertion lands between the two deleted stretches
    assert result == "X9"


def test_a_removal_beside_a_blank_line_still_takes_the_insertions_where_its_ranges_meet() -> None:
    # given — the only binding stands below a blank line at the opening bracket, so its removal is two touching ranges
    template = '[\n\n    { "key": "c", "command": "x" },\n]'

    # when — it is replaced by two bindings, which are inserted where those ranges meet
    rendered = _render(template, [{"key": "d", "command": "z"}, {"key": "f", "command": "z"}])

    # then — both land, and the blank line against the bracket goes with the removed binding
    assert rendered == '[\n    {"key": "d", "command": "z"},\n    {"key": "f", "command": "z"},\n]'


@pytest.mark.parametrize(
    "edits",
    [
        [jsonc._Edit(0, 6, ""), jsonc._Edit(3, 3, "X")],
        [jsonc._Edit(0, 6, ""), jsonc._Edit(3, 9, "")],
    ],
    ids=["insertion-inside", "reaching-past"],
)
def test_an_edit_that_would_be_dropped_refuses_the_render(edits: list[Any]) -> None:
    # when & then — the edit starts inside an earlier one and carries text or reaches past it, so it is not skipped
    with pytest.raises(RenderRefused) as refused:
        jsonc._apply_edits("0123456789", edits)
    assert refused.value.reason == jsonc.EDIT_OVERLAP_REFUSED


@pytest.mark.parametrize(
    ("edits", "expected"),
    [
        ([jsonc._Edit(0, 6, ""), jsonc._Edit(2, 4, "")], "6789"),
        ([jsonc._Edit(0, 6, "Y"), jsonc._Edit(2, 4, "")], "Y6789"),
    ],
    ids=["removal-inside-a-removal", "removal-inside-a-replacement"],
)
def test_a_removal_inside_an_earlier_edit_is_skipped_harmlessly(edits: list[Any], expected: str) -> None:
    # when — the second edit removes only text the first one already took
    result = jsonc._apply_edits("0123456789", edits)

    # then
    assert result == expected


def _row(key: str, comma: str = ",") -> str:
    """One one-line binding with the `c` command, indented as the tests' templates are."""
    return f'    {{"key": "{key}", "command": "c"}}{comma}'


def _keys(*keys: str) -> list[dict[str, object]]:
    return [{"key": key, "command": "c"} for key in keys]


@pytest.mark.parametrize(
    ("template", "keys", "expected"),
    [
        ("[\n\n" + _row("x") + "\n" + _row("a", "") + "\n]", ("a",), "[\n" + _row("a", "") + "\n]"),
        (
            "[\n\n" + _row("x") + "\n" + _row("a", "") + "\n]",
            ("a", "x"),
            "[\n" + _row("a") + "\n" + _row("x", "") + "\n]",
        ),
        (
            "[\n\n" + _row("x") + "\n" + _row("a", "") + "\n]",
            ("n", "a"),
            "[\n" + _row("n") + "\n" + _row("a", "") + "\n]",
        ),
        (
            "[ // cols\n\n" + _row("x") + "\n" + _row("a", "") + "\n]",
            ("a",),
            "[ // cols\n" + _row("a", "") + "\n]",
        ),
        (
            "[ /* a\n\n   b */\n\n" + _row("x") + "\n" + _row("a", "") + "\n]",
            ("a",),
            "[ /* a\n\n   b */\n" + _row("a", "") + "\n]",
        ),
        (
            "[\n\n    // note on x\n" + _row("x") + "\n" + _row("a", "") + "\n]",
            ("a",),
            "[\n" + _row("a", "") + "\n]",
        ),
        (
            "[\n\n" + _row("x") + "\n\n" + _row("a", "") + "\n]",
            ("a",),
            "[\n" + _row("a", "") + "\n]",
        ),
    ],
    ids=[
        "removed",
        "moved",
        "replaced",
        "comment-on-bracket",
        "block-comment-on-bracket",
        "note-removed-too",
        "blank-after",
    ],
)
def test_a_blank_line_after_the_opening_bracket_goes_with_the_first_binding_an_edit_takes(
    template: str, keys: tuple[str, ...], expected: str
) -> None:
    # when — the binding right under the blank line is removed, moved away, or replaced
    rendered = _render(template, _keys(*keys))

    # then — the blank line would sit against the opening bracket, so it goes with the binding
    assert rendered == expected


@pytest.mark.parametrize(
    ("template", "keys", "expected"),
    [
        (
            "[\n\n" + _row("a") + "\n" + _row("x", "") + "\n]",
            ("a",),
            "[\n\n" + _row("a", "") + "\n]",
        ),
        (
            "[\n\n" + _row("a", "") + "\n]",
            ("n", "a"),
            "[\n\n" + _row("n") + "\n" + _row("a", "") + "\n]",
        ),
        (
            "[\n    // S\n\n" + _row("x") + "\n" + _row("a", "") + "\n]",
            ("a",),
            "[\n    // S\n\n" + _row("a", "") + "\n]",
        ),
    ],
    ids=["later-binding-removed", "binding-added-above", "title-between"],
)
def test_a_blank_line_after_the_opening_bracket_that_no_edit_touches_stays(
    template: str, keys: tuple[str, ...], expected: str
) -> None:
    # when — an edit elsewhere, an addition above the first binding, or a removal below a section title
    rendered = _render(template, _keys(*keys))

    # then — the blank line is the user's and nothing took the binding it was set before
    assert rendered == expected


def test_a_blank_line_after_the_opening_bracket_goes_with_the_first_binding_in_crlf() -> None:
    # given — the same layout as the removal case, with CRLF line endings
    template = ("[\n\n" + _row("x") + "\n" + _row("a", "") + "\n]").replace("\n", "\r\n")

    # when
    rendered = _render(template, _keys("a"))

    # then
    assert rendered == "[\r\n" + _row("a", "") + "\r\n]"


@st.composite
def _lead_case(draw: st.DrawFn) -> tuple[str, bool, list[bool]]:
    """A template of one-line bindings, optionally with a blank line after `[`, and which of the bindings are kept."""
    count = draw(st.integers(min_value=1, max_value=5))
    lead = draw(st.booleans())
    keep = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    newline = draw(st.sampled_from(["\n", "\r\n"]))
    rows = [_row(f"k{index}", "," if index < count - 1 else "") for index in range(count)]
    return newline.join(["[", *([""] if lead else []), *rows, "]"]), lead, keep


@given(case=_lead_case())
def test_a_blank_line_after_the_opening_bracket_stays_exactly_when_the_first_binding_stays(case) -> None:
    # given — bindings after an optional blank line, some of them removed
    template, lead, keep = case
    value = _keys(*[f"k{index}" for index, kept in enumerate(keep) if kept])

    # when
    rendered = _try_render(template, value)

    # then — a render exists, and a blank line follows `[` only if the template had one and the first binding is kept
    assert rendered is not None
    lines = rendered.replace("\r\n", "\n").split("\n")
    assert (len(lines) > 1 and lines[1].strip() == "") == (lead and keep[0])


@pytest.mark.parametrize(
    ("template", "value", "rules", "expected"),
    [
        (
            '{\n  "r": [\n\n    80,\n    100\n  ]\n}',
            {"r": [90, 100]},
            {},
            '{\n  "r": [\n    90,\n    100\n  ]\n}',
        ),
        (
            '[\n\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }\n]',
            [_a("z"), _b("y")],
            {(): RULE},
            '[\n\n    {"key": "a", "command": "z"},\n    { "key": "b", "command": "y" }\n]',
        ),
    ],
    ids=["rule-less-array-loses-the-blank-line", "ruled-array-keeps-it"],
)
def test_the_first_value_of_an_array_changing_keeps_or_drops_the_blank_line_after_the_bracket_by_its_rule(
    template: str, value: object, rules: dict[Any, Any], expected: str
) -> None:
    # when — the first value changes: without a rule that is a deletion and an insertion, with a rule the same group
    # is rewritten where it stands
    rendered = ADAPTER.render(tree_from_value(value), template, rules)

    # then — this pins the known behavior, not a recommended one: the blank line after `[` goes in the first case only
    assert rendered == expected


def test_two_blank_lines_after_the_opening_bracket_both_stay_when_the_first_binding_goes() -> None:
    # given — the template already had two blank lines after `[`
    template = '[\n\n\n    { "key": "a", "command": "x" },\n    { "key": "b", "command": "y" }\n]'

    # when — the first binding is removed
    rendered = _render(template, [_b("y")])

    # then — this pins the known behavior, not a recommended one: only a blank line right against `[` goes with it
    assert rendered == '[\n\n\n    { "key": "b", "command": "y" }\n]'


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_removing_every_binding_under_a_section_title_leaves_no_blank_line_before_the_bracket(newline: str) -> None:
    # given — a section title, then two bindings separated by a blank line, all of which are removed
    template = '[\n    // section 0\n\n    { "key": "e", "command": "x" },\n\n    { "key": "g", "command": "y" }\n]'

    # when
    rendered = _render(template.replace("\n", newline), [])

    # then — the title stays and no blank line is left against the closing bracket
    assert rendered == f"[{newline}    // section 0{newline}]"


def test_a_block_comment_spanning_lines_after_the_opening_bracket_is_read_as_the_bracket_line() -> None:
    # given — the opening bracket is followed by a block comment that runs over two lines, then the first binding
    template = '[ /* a\n   b */\n    { "key": "x", "command": "1" },\n\n    { "key": "y", "command": "2" }\n]'

    # when — the first binding is removed
    rendered = _render(template, [_y("2")])

    # then — the blank line after it goes too, because the block is still the array's opening line
    assert rendered == '[ /* a\n   b */\n    { "key": "y", "command": "2" }\n]'


def test_a_comment_inside_a_rule_less_inline_array_refuses_with_the_general_reason() -> None:
    # given — an inline array with a comment in it; its value changes, so it is rewritten whole
    template = '{\n  "editor.rulers": [80, /* why */ 120]\n}'

    # when & then — the refusal is the general one: a setting, not a binding, is what changed
    with pytest.raises(RenderRefused) as refused:
        ADAPTER.render(tree_from_value({"editor.rulers": [81, 120]}), template)
    assert refused.value.reason == VALUE_WITH_NOTE_REFUSED
    assert refused.value.reason != CHANGED_WITH_NOTE_REFUSED


def test_a_comment_inside_an_object_member_refuses_with_the_general_reason() -> None:
    # given — an object member with a comment inside it changes its value
    template = '{\n  "x": {"a": 1, /* c */ "b": 2}\n}'

    # when & then
    with pytest.raises(RenderRefused) as refused:
        ADAPTER.render(tree_from_value({"x": {"a": 1, "b": 3}}), template)
    assert refused.value.reason == VALUE_WITH_NOTE_REFUSED


def test_a_comma_first_layout_with_a_comment_refuses_with_the_general_reason() -> None:
    # given — keybindings with each comma at the start of its line, and a comment inside the first binding
    template = '[\n  {"key": "a", "command": "x" /* c */}\n  , {"key": "b", "command": "y"}\n]'

    # when & then — the array is rewritten whole, so the refusal is the general one, not a binding's
    with pytest.raises(RenderRefused) as refused:
        ADAPTER.render(tree_from_value([_a("x2"), _b("y")]), template)
    assert refused.value.reason == VALUE_WITH_NOTE_REFUSED


def test_a_blank_line_after_an_opening_bracket_with_a_comment_is_removed_with_the_element() -> None:
    # given — the opening bracket carries a comment, and a blank line follows the first binding
    template = '[ // cols\n    { "key": "a", "command": "x" },\n\n    { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_b("y")])

    # then — the bracket still counts as opening the array, so no blank line is left after it
    assert rendered == '[ // cols\n    { "key": "b", "command": "y" }\n]'


def test_a_comment_that_ends_in_a_bracket_is_not_an_opening_bracket() -> None:
    # given — the line before a removed binding ends with `[` only inside a comment
    template = (
        '[\n    { "key": "z", "command": "w" }, // see [\n    { "key": "a", "command": "x" },\n\n'
        '    { "key": "b", "command": "y" }\n]'
    )

    # when
    rendered = _render(template, [_z("w"), _b("y")])

    # then — the blank line between the removed binding and `b` is kept, because the line above is not a bracket
    assert rendered == '[\n    { "key": "z", "command": "w" }, // see [\n\n    { "key": "b", "command": "y" }\n]'


def test_a_multi_line_block_note_moves_with_its_binding() -> None:
    # given — a block note spread over two lines documents `a`, and the merge moves `a` below `c`
    template = (
        '[\n    /* first\n       second */\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_c("z"), _a("x")])

    # then — the whole two-line block travels with `a`
    assert rendered == (
        '[\n    { "key": "c", "command": "z" },\n    /* first\n       second */\n    { "key": "a", "command": "x" }\n]'
    )


def test_a_multi_line_block_note_goes_with_its_binding_when_that_binding_is_removed() -> None:
    # given — the same block note documents `a`, which is removed
    template = (
        '[\n    /* first\n       second */\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'
    )

    # when
    rendered = _render(template, [_c("z")])

    # then — nothing of the block is left behind
    assert rendered == '[\n    { "key": "c", "command": "z" }\n]'


def _binding_array(count: int) -> tuple[str, list[Any]]:
    """An array of `count` bindings, each with a note above it, and the same bindings with the first moved last."""
    lines = ["["]
    for index in range(count):
        lines.append(f"    // note {index}")
        comma = "," if index < count - 1 else ""
        lines.append(f'    {{ "key": "k{index}", "command": "c{index}" }}{comma}')
    lines.append("]")
    value: list[Any] = [{"key": f"k{index}", "command": f"c{index}"} for index in range(1, count)]
    return "\n".join(lines), [*value, {"key": "k0", "command": "c0"}]


def _best_render_seconds(count: int) -> float:
    """The fastest of three renders of a `count`-binding array, each binding moved by the merge.

    It is measured in CPU time of this process, not wall-clock time: other work on the machine stretches wall-clock time
    of a short render several times over, and the ratio of two of them with it.
    """
    template, value = _binding_array(count)
    timings = []
    for _ in range(3):
        started = time.process_time()
        _render(template, value)
        timings.append(time.process_time() - started)
    return min(timings)


def test_moving_a_binding_scales_with_the_number_of_bindings_not_their_square() -> None:
    # when — the same merge at 400 and at 3200 bindings (eight times the size); the CPU-time ratio is compared, so the
    # test depends neither on the machine's speed nor much on its load
    ratio = _best_render_seconds(3200) / _best_render_seconds(400)

    # then — linear work gives a ratio near 8; the old quadratic scan gave about 50 on the same machine
    assert ratio < 20


# --- the rich property: every kind of note, with section titles, `when`, additions, reorders and changes ----------


@st.composite
def _rich_case(draw: st.DrawFn) -> tuple[str, list[dict[str, object]], list[str], list[int], list[dict[str, object]]]:
    """A template with notes of all three kinds, a section title, `when` fields, and a random merged target.

    Returns the template, the base bindings, each base binding's text fragment, and the indexes of the base bindings
    the target keeps unchanged (the ones whose notes must survive).
    """
    base = draw(st.lists(st.sampled_from(_POOL), unique=True, min_size=1, max_size=4))
    flags = draw(
        st.lists(
            st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans()),
            min_size=len(base),
            max_size=len(base),
        )
    )
    bindings: list[dict[str, object]] = []
    fragments: list[str] = []
    blank_between = draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base)))
    newline = draw(st.sampled_from(["\n", "\r\n"]))
    lines = ["["]
    for index, ((key, command), (inner, inline, section, has_when)) in enumerate(zip(base, flags, strict=True)):
        binding: dict[str, object] = {"key": key, "command": command}
        body = f'"key": "{key}", ' + (f"/* in {index} */ " if inner else "") + f'"command": "{command}"'
        if has_when:
            binding["when"] = f"w{index}"
            body += f', "when": "w{index}"'
        bindings.append(binding)
        fragments.append(body)
        if section:
            lines.extend(["", f"    // section {index}", ""])
        elif blank_between[index] and index > 0:
            lines.append("")
        lines.append(f"    // note {index}")
        comma = "," if index < len(base) - 1 else ""
        lines.append(f"    {{{body}}}{comma}" + (f" // inl {index}" if inline else ""))
    lines.append("]")
    template = newline.join(lines)

    kept = draw(st.lists(st.integers(min_value=0, max_value=len(base) - 1), unique=True, max_size=len(base)))
    order = draw(st.permutations(kept))
    kinds = draw(st.lists(st.sampled_from(("keep", "args", "when")), min_size=len(order), max_size=len(order)))
    value: list[dict[str, object]] = []
    unchanged: list[int] = []
    for index, kind in zip(order, kinds, strict=True):
        item = dict(bindings[index])
        if kind == "args":
            item["args"] = 1
        elif kind == "when":
            # A gained or changed `when` is a different binding: its note still belongs to it, but it is not unchanged.
            item["when"] = f"w{index}x"
        else:
            unchanged.append(index)
        value.append(item)
    # Bindings the template never had are added at drawn positions, so additions land among the kept ones.
    available = [pair for pair in _POOL if pair not in base]
    added = draw(st.lists(st.sampled_from(available), unique=True, max_size=2))
    value.extend({"key": key, "command": command} for key, command in added)
    value = draw(st.permutations(value))
    return template, bindings, fragments, unchanged, value


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_a_blank_line_between_two_kept_bindings_survives_a_removal_between_them(newline: str) -> None:
    # given — `x` and `y` sit between `a` and `b` with one blank line between them; both are removed
    template = (
        '[\n    { "key": "a", "command": "a" },\n    { "key": "x", "command": "x" },\n\n'
        '    { "key": "y", "command": "y" },\n    { "key": "b", "command": "b" }\n]'
    )

    # when
    rendered = _render(template.replace("\n", newline), [{"key": "a", "command": "a"}, {"key": "b", "command": "b"}])

    # then — `a` and `b` are kept, so the blank line that separated them stays as one line
    kept_a = '    { "key": "a", "command": "a" },'
    kept_b = '    { "key": "b", "command": "b" }'
    assert rendered == f"[{newline}{kept_a}{newline}{newline}{kept_b}{newline}]"


def test_a_removal_between_kept_bindings_and_an_insertion_keeps_the_blank_line_before_the_new_binding() -> None:
    # given — the same layout; `x` and `y` are removed and a new `z` is merged in after `a`
    template = (
        '[\n    { "key": "a", "command": "a" },\n    { "key": "x", "command": "x" },\n\n'
        '    { "key": "y", "command": "y" },\n    { "key": "b", "command": "b" }\n]'
    )

    # when
    rendered = _render(
        template, [{"key": "a", "command": "a"}, {"key": "z", "command": "z"}, {"key": "b", "command": "b"}]
    )

    # then — the blank line stays between `a` and the new binding, and no line is left against a bracket
    assert rendered == (
        '[\n    { "key": "a", "command": "a" },\n\n'
        '    {"key": "z", "command": "z"},\n    { "key": "b", "command": "b" }\n]'
    )


def test_a_blank_line_inside_a_block_comment_after_the_opening_bracket_is_not_a_blank_line() -> None:
    # given — the opening bracket is followed by a block comment with an empty line inside it
    template = '[ /* a\n\n   b */\n    { "key": "x", "command": "1" },\n\n    { "key": "y", "command": "2" }\n]'

    # when — `x` is removed
    rendered = _render(template, [{"key": "y", "command": "2"}])

    # then — the blank line after `x` goes, since the line above it is the array's opening line
    assert rendered == '[ /* a\n\n   b */\n    { "key": "y", "command": "2" }\n]'


@given(case=_rich_case())
def test_a_rich_render_parses_back_exactly_or_refuses(case) -> None:
    # given
    template, bindings, _, _, value = case

    # when
    rendered = _try_render(template, value)

    # then — a render parses back to the merged value, never leaves two blank lines side by side, and never leaves a
    # blank line against either bracket, unless the template had one there. A merge that changes nothing keeps the
    # layout exactly, blank lines and line endings included. A CRLF template renders with CRLF only.
    opens_blank = re.compile(r"\[[ \t]*\r?\n[ \t]*\r?\n")
    crlf_only = "\r\n" not in template or re.search(r"(?<!\r)\n", rendered or "") is None
    assert rendered is None or (
        ADAPTER.parse(rendered) == tree_from_value(value)
        and not _has_doubled_blank(rendered)
        and re.search(r"\r?\n[ \t]*\r?\n[ \t]*\]", rendered) is None
        and (opens_blank.search(template) is not None or opens_blank.search(rendered) is None)
        and (value != bindings or rendered == template)
        and crlf_only
    )


@settings(max_examples=400, deadline=None)
@given(case=_rich_case())
def test_an_unchanged_binding_keeps_its_note_above_it_and_its_inner_and_inline_notes(case) -> None:
    # given
    template, _bindings, fragments, unchanged, value = case

    # when
    rendered = _try_render(template, value)

    # then — checked as text in place: the note sits directly above the binding's own line, not merely somewhere
    text = "" if rendered is None else rendered.replace("\r\n", "\n")
    missing = [
        index
        for index in unchanged
        if rendered is not None
        and (
            f"// note {index}\n    {{{fragments[index]}" not in text
            or (f"/* in {index} */" in template and f"/* in {index} */" not in text)
            or (f"// inl {index}" in template and f"// inl {index}" not in text)
        )
    ]
    assert missing == []


def _refusal_share(samples: int, seed: int) -> tuple[int, int]:
    """Refused and total renders over a fixed sample of the notes generator, from one seeded source."""
    rng = random.Random(seed)
    refused = 0
    for _ in range(samples):
        base = rng.sample(_POOL, rng.randint(1, 4))
        lines = ["["]
        for index, (key, command) in enumerate(base):
            comma = "," if index < len(base) - 1 else ""
            lines.append(f"    // note {index}")
            lines.append(f'    {{"key": "{key}", "command": "{command}"}}{comma}')
        lines.append("]")
        kept = rng.sample(base, rng.randint(0, len(base)))
        value: list[dict[str, object]] = [{"key": key, "command": command} for key, command in kept]
        if rng.random() < 0.5 and value:
            value[0]["args"] = 1
        refused += _try_render("\n".join(lines), value) is None
    return refused, samples


def test_refusals_are_not_the_majority_of_generated_renders() -> None:
    # given — a fixed, seeded sample, so the count is the same on every run
    samples = 200

    # when
    refused, total = _refusal_share(samples, seed=7)

    # then
    assert refused * 2 < total


def _rule_less_template(values: list[int]) -> str:
    """A bulleted array of plain values, each under its own note."""
    lines = ["["]
    for index, value in enumerate(values):
        comma = "," if index < len(values) - 1 else ""
        lines.append(f"    // note {index}")
        lines.append(f"    {value}{comma}")
    lines.append("]")
    return "\n".join(lines)


@given(case=st.data())
def test_a_rule_less_render_never_drops_a_note_when_it_succeeds(case: st.DataObject) -> None:
    # given — an array with no rule, so each value is its own identity; notes sit above every value
    values = case.draw(st.lists(st.integers(min_value=0, max_value=9), unique=True, min_size=1, max_size=5))
    target = case.draw(st.lists(st.sampled_from(values), unique=True, max_size=5))
    template = _rule_less_template(values)

    # when
    rendered = _try_plain_render(template, target)

    # then — no note is dropped, and each kept value still has its own note directly above its line
    text = "" if rendered is None else rendered
    dropped = [index for index in range(len(values)) if rendered is not None and f"// note {index}" not in text]
    misplaced = [
        f"// note {values.index(value)}\n    {value}"
        for value in target
        if rendered is not None and f"// note {values.index(value)}\n    {value}" not in text
    ]
    assert dropped == []
    assert misplaced == []


# --- blank lines around removals and insertions: a property over chunks of titles and noted bindings ---------------


@st.composite
def _gap_case(draw: st.DrawFn) -> tuple[str, list[dict[str, Any]], list[bool], str, list[int], list[tuple[str, str]]]:
    """A template of section titles and bindings (each binding may have a note directly above it), with blank lines
    between chunks at random, plus which bindings the target keeps and which bindings it adds.

    Returns the template, the chunks in template order, the blank flag before each chunk after the first, the newline,
    the chunk indexes of the kept bindings, and the added pairs (the merged value's bindings the template never had).
    """
    kinds = draw(st.lists(st.sampled_from(("binding", "binding", "binding", "title")), min_size=1, max_size=9))
    count = kinds.count("binding")
    # Every binding gets its own key, so no two can share a group and each one is independently kept or removed.
    keys = draw(st.permutations(range(count + 3)))
    pairs = [(f"k{key}", "v") for key in keys[:count]]
    notes = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    blanks = draw(st.lists(st.booleans(), min_size=len(kinds) - 1, max_size=len(kinds) - 1))
    # A title with a binding right below it, and no blank line between them, is that binding's note; a section title is
    # set off by a blank line.
    blanks = [flag or kinds[position] == "title" for position, flag in enumerate(blanks)]
    newline = draw(st.sampled_from(["\n", "\r\n"]))
    keep_flags = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    additions = draw(st.lists(st.sampled_from(keys[count:]), unique=True, max_size=2))

    chunks: list[dict[str, Any]] = []
    binding_slot = 0
    for position, kind in enumerate(kinds):
        if kind == "title":
            chunks.append({"kind": "title", "index": position, "line": f"    // S{position}"})
            continue
        key, command = pairs[binding_slot]
        chunks.append(
            {
                "kind": "binding",
                "index": position,
                "key": key,
                "command": command,
                "note": notes[binding_slot],
                "body": f'{{"key": "{key}", "command": "{command}"}}',
            }
        )
        binding_slot += 1

    lines = ["["]
    for position, chunk in enumerate(chunks):
        if position > 0 and blanks[position - 1]:
            lines.append("")
        if chunk["kind"] == "title":
            lines.append(chunk["line"])
            continue
        if chunk["note"]:
            lines.append(f"    // n{chunk['index']}")
        later_binding = any(later["kind"] == "binding" for later in chunks[position + 1 :])
        lines.append(f"    {chunk['body']}{',' if later_binding else ''}")
    lines.append("]")

    kept = [chunk["index"] for chunk in chunks if chunk["kind"] == "binding"]
    kept = [index for index, flag in zip(kept, keep_flags, strict=True) if flag]
    # An added binding has a key the template does not, so it is not paired with a template binding in the same group.
    added = [(f"k{key}", "w") for key in additions]
    return newline.join(lines), chunks, blanks, newline, kept, added


def _output_lines(rendered: str) -> list[str]:
    return rendered.replace("\r\n", "\n").split("\n")


def _line_of(lines: list[str], text: str) -> int | None:
    """The index of the line whose code is `text`, ignoring the trailing comma the line may carry."""
    return next((index for index, line in enumerate(lines) if line.strip().rstrip(",") == text), None)


def _start_line_of(lines: list[str], chunk: dict[str, Any]) -> int | None:
    """The line where a chunk starts: its note when it has one, its own line otherwise."""
    if chunk["kind"] == "title":
        return _line_of(lines, chunk["line"].strip())
    if chunk["note"]:
        return _line_of(lines, f"// n{chunk['index']}")
    return _line_of(lines, chunk["body"])


def _survivor_gap_violations(
    chunks: list[dict[str, Any]], blanks: list[bool], rendered: str, kept: list[int]
) -> list[str]:
    """Each pair of survivors (titles and kept bindings) with a removed binding between them, checked for blank lines.

    The output gap between them must hold exactly one blank line when the template had any blank line in the gap, and
    none otherwise, and nothing but blank lines.
    """
    lines = _output_lines(rendered)
    survivors = [chunk for chunk in chunks if chunk["kind"] == "title" or chunk["index"] in kept]
    problems: list[str] = []
    for before, after in itertools.pairwise(survivors):
        between = [chunk for chunk in chunks if before["index"] < chunk["index"] < after["index"]]
        if not any(chunk["kind"] == "binding" and chunk["index"] not in kept for chunk in between):
            continue
        expected = 1 if any(blanks[before["index"] : after["index"]]) else 0
        end = _line_of(lines, before["line"].strip() if before["kind"] == "title" else before["body"])
        start = _start_line_of(lines, after)
        if end is None or start is None or start <= end:
            problems.append(f"missing survivor: {before['index']} -> {after['index']}")
            continue
        gap = lines[end + 1 : start]
        if any(line.strip() for line in gap) or len(gap) != expected:
            problems.append(f"gap {before['index']} -> {after['index']}: {gap!r} expected {expected} blank")
    return problems


def _title_above_insertion_violations(chunks: list[dict[str, Any]], blanks: list[bool], rendered: str) -> list[str]:
    """Each title that had a blank line above it and a binding above it in the template keeps a blank line above it.

    With every binding removed and new ones added, the additions land above the first binding, so the blank line the
    title had after a removed binding must still separate it from what was inserted.
    """
    lines = _output_lines(rendered)
    problems: list[str] = []
    for chunk in chunks:
        if chunk["kind"] != "title" or chunk["index"] == 0 or not blanks[chunk["index"] - 1]:
            continue
        if not any(earlier["kind"] == "binding" for earlier in chunks[: chunk["index"]]):
            continue
        at = _line_of(lines, chunk["line"].strip())
        if at is None or at == 0 or lines[at - 1].strip():
            problems.append(f"title {chunk['index']} lost its blank line above it")
    return problems


@settings(max_examples=500, deadline=None)
@given(case=_gap_case())
def test_a_blank_line_between_two_survivors_stays_one_line_when_removals_sit_between(case) -> None:
    # given — the target keeps some bindings and removes others; nothing is added or reordered
    template, chunks, blanks, _newline, kept, _added = case
    value = [
        {"key": chunk["key"], "command": chunk["command"]}
        for chunk in chunks
        if chunk["kind"] == "binding" and chunk["index"] in kept
    ]

    # when
    rendered = _try_render(template, value)

    # then — a render exists, and every gap between two survivors that had a removal between them holds one blank line
    # if the template had any blank line there, and none otherwise
    assert rendered is not None
    assert _survivor_gap_violations(chunks, blanks, rendered, kept) == []


@settings(max_examples=500, deadline=None)
@given(case=_gap_case())
def test_a_blank_line_above_a_title_stays_when_the_bindings_above_it_are_removed_and_one_is_added(case) -> None:
    # given — every binding is removed and a new one is merged in, so the new binding lands above the first removal
    template, chunks, blanks, _newline, _kept, added = case
    assume(added and any(chunk["kind"] == "binding" for chunk in chunks))

    # when
    rendered = _try_render(template, [{"key": key, "command": command} for key, command in added])

    # then — each title with a blank line above it in the template still has a blank line above it in the output
    assert rendered is not None
    assert _title_above_insertion_violations(chunks, blanks, rendered) == []
