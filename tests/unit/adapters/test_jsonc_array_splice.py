# SPDX-License-Identifier: AGPL-3.0-or-later
"""Element-level JSONC rendering: a changed array keeps the comments of the elements it did not touch."""

from __future__ import annotations

import os
import random
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


def test_a_comment_above_a_removed_element_stays_where_it_is() -> None:
    # given — `// why x` sits above x; the merge removes x and puts a different binding y in its place
    template = '[\n    // why x\n    { "key": "a", "command": "x" },\n    { "key": "c", "command": "z" }\n]'

    # when
    rendered = _render(template, [_b("y"), _c("z")])

    # then — the note is not deleted: it stays above the line that follows, rather than being guessed onto y
    assert rendered == '[\n    // why x\n    {"key": "b", "command": "y"},\n    { "key": "c", "command": "z" }\n]'


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
    """A keybindings template with notes above, beside, and inside each binding; a random reordered merged target."""
    base = draw(st.lists(st.sampled_from(_POOL), unique=True, min_size=1, max_size=5))
    with_inline = draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base)))
    lines = ["["]
    for index, (key, command) in enumerate(base):
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
def test_a_note_above_a_binding_is_never_dropped_and_an_unchanged_binding_keeps_its_notes(
    case: tuple[str, list[tuple[str, str]], list[dict]],
) -> None:
    # given
    template, base, value = case

    # when
    rendered = _try_render(template, value)

    # then — a note above any binding stays with the array; an unchanged binding keeps its inner and inline notes
    unchanged = {f"{binding['key']}/{binding['command']}" for binding in value if "args" not in binding}
    rendered_ok = rendered is not None
    text = "" if rendered is None else rendered
    dropped_above = [index for index in range(len(base)) if rendered_ok and f"// note {index}" not in text]
    lost_inside = [
        index
        for index, (key, command) in enumerate(base)
        if rendered_ok and f"{key}/{command}" in unchanged and _inner_notes_lost(index, text, template)
    ]
    assert dropped_above == []
    assert lost_inside == []


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


def test_a_rule_array_changes_a_binding_whose_when_changed_and_keeps_its_upper_note() -> None:
    # given — the binding gains a `when`, so it is no longer the same binding; its note above stays, its inline one goes
    template = (
        '[\n    // upper note\n    { "key": "a", "command": "x" }, // inline note\n'
        '    { "key": "c", "command": "z" }\n]'
    )
    gained_when = {"key": "a", "command": "x", "when": "editorFocus"}

    # when
    rendered = _render(template, [gained_when, _c("z")])

    # then
    assert "// upper note" in rendered
    assert "// inline note" not in rendered
    assert ADAPTER.parse(rendered) == tree_from_value([gained_when, _c("z")])


def test_a_comma_alone_on_its_line_is_not_spliced_and_stays_valid_json() -> None:
    # given — a comma starts its own line between two bindings
    template = '[\n  { "key": "a", "command": "x" }\n  ,\n  { "key": "b", "command": "y" }\n]'

    # when
    rendered = _render(template, [_b("y"), _a("x")])

    # then — the array is rewritten whole, so the result parses to the merged order
    assert ADAPTER.parse(rendered) == tree_from_value([_b("y"), _a("x")])


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
        lines.append(f"    // note {index}")
        comma = "," if index < len(base) - 1 else ""
        lines.append(f"    {{{body}}}{comma}" + (f" // inl {index}" if inline else ""))
    lines.append("]")
    template = "\n".join(lines)

    kept = draw(st.lists(st.integers(min_value=0, max_value=len(base) - 1), unique=True, max_size=len(base)))
    order = draw(st.permutations(kept))
    changed = draw(st.lists(st.booleans(), min_size=len(order), max_size=len(order)))
    value: list[dict[str, object]] = []
    unchanged: list[int] = []
    for index, change in zip(order, changed, strict=True):
        item = dict(bindings[index])
        if change:
            item["args"] = 1
        else:
            unchanged.append(index)
        value.append(item)
    return template, bindings, fragments, unchanged, value


@given(case=_rich_case())
def test_a_rich_render_parses_back_exactly_or_refuses(case) -> None:
    # given
    template, _, _, _, value = case

    # when
    rendered = _try_render(template, value)

    # then
    assert rendered is None or ADAPTER.parse(rendered) == tree_from_value(value)


@given(case=_rich_case())
def test_an_unchanged_binding_keeps_its_note_above_it_and_its_inner_and_inline_notes(case) -> None:
    # given
    template, _bindings, fragments, unchanged, value = case

    # when
    rendered = _try_render(template, value)

    # then — checked as text in place: the note sits directly above the binding's own line, not merely somewhere
    text = "" if rendered is None else rendered
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

    # then
    text = "" if rendered is None else rendered
    dropped = [index for index in range(len(values)) if rendered is not None and f"// note {index}" not in text]
    assert dropped == []
