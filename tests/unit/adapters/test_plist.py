# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for the property-list adapter.

The fixture uses key names from iTerm2's preferences domain with invented values. The adapter's contract differs from
JSONC's: render returns the one canonical XML form, so the round trip is `render(parse(text)) == canonical(text)`.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.adapters.edits import apply_edits
from syncintosh.adapters.plist import PlistAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ContentParseError
from syncintosh.core.keyed_array import ArrayRule, element_identity
from syncintosh.core.merge import merge_tree
from syncintosh.core.plist_xml import BINARY_REFUSED, ROOT_REFUSED, SUBSET_REFUSED, canonical_plist
from syncintosh.core.tree import Array, EmptyObject, Object, Scalar, Tree

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "plist"
ADAPTER = PlistAdapter()


def _fixture() -> str:
    return (FIXTURES / "prefs.plist").read_text(encoding="utf-8")


def _xml(value: dict[str, object]) -> str:
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


def _merge(base: str, local: str, remote: str):
    return merge_content(base.encode(), local.encode(), remote.encode(), TextMergeDriver(), ADAPTER)


def test_a_canonical_document_round_trips_to_the_same_bytes() -> None:
    # given
    text = canonical_plist(_fixture().encode()).decode()

    # when
    rendered = ADAPTER.render(ADAPTER.parse(text), text)

    # then
    assert rendered == text


def test_a_tab_indented_export_renders_in_the_canonical_form() -> None:
    # given
    text = _fixture()

    # when
    rendered = ADAPTER.render(ADAPTER.parse(text), text)

    # then
    assert rendered == canonical_plist(text.encode()).decode()


def test_each_plist_type_has_its_own_kind() -> None:
    # when
    tree = ADAPTER.parse(_fixture())

    # then
    assert {path[-1]: tree.value(path) for path in tree if path[-1].startswith(("Sample", "Apple", "Hotkey"))} == {
        "SampleBlob": Scalar("data", "AQIDBA=="),
        "SampleDate": Scalar("date", "2024-01-02T03:04:05Z"),
        "SampleReal": Scalar("float", 1.0),
        "AppleAntiAliasingThreshold": Scalar("int", 4),
        "HotkeyMigratedFromSingleToMulti": Scalar("bool", True),
    }


def test_data_and_a_string_with_the_same_characters_are_different_values() -> None:
    # given
    data_text = _xml({"k": b"\x01\x02\x03\x04"})
    string_text = _xml({"k": "AQIDBA=="})

    # when
    as_data = ADAPTER.parse(data_text)
    as_string = ADAPTER.parse(string_text)

    # then
    assert as_data.value(("k",)) != as_string.value(("k",))


def test_a_real_and_an_integer_stay_distinct_through_a_round_trip() -> None:
    # given
    text = _xml({"i": 1, "r": 1.0})

    # when
    again = ADAPTER.parse(ADAPTER.render(ADAPTER.parse(text), text))

    # then
    assert (again.value(("i",)), again.value(("r",))) == (Scalar("int", 1), Scalar("float", 1.0))


def test_data_and_date_come_back_as_the_same_plist_values() -> None:
    # given
    text = _xml({"b": b"\x00\xff", "d": datetime(2020, 2, 29, 23, 59, 59), "in": [b"x", datetime(2001, 1, 1)]})

    # when
    rendered = ADAPTER.render(ADAPTER.parse(text), text)

    # then
    assert plistlib.loads(rendered.encode()) == plistlib.loads(text.encode())


def test_nested_dicts_become_key_paths_and_empty_ones_survive() -> None:
    # when
    tree = ADAPTER.parse(_xml({"a": {"b": {"c": 1}}, "e": {}, "l": [{"x": 1}]}))

    # then
    assert dict(tree.entries) == {
        ("a", "b", "c"): Scalar("int", 1),
        ("e",): EmptyObject(),
        ("l",): Array((Object((("x", Scalar("int", 1)),)),)),
    }


def test_an_empty_document_round_trips() -> None:
    # given
    text = _xml({})

    # when
    tree = ADAPTER.parse(text)

    # then
    assert (dict(tree.entries), ADAPTER.render(tree, text)) == ({(): EmptyObject()}, text)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY).decode("latin-1"), BINARY_REFUSED),
        (plistlib.dumps(["a"]).decode(), ROOT_REFUSED),
        ('<?xml version="1.0"?><!DOCTYPE plist [<!ENTITY x "y">]><plist version="1.0"><dict/></plist>', SUBSET_REFUSED),
    ],
    ids=["binary", "array-root", "internal-subset"],
)
def test_input_the_adapter_cannot_write_back_is_refused(text: str, reason: str) -> None:
    # when & then
    with pytest.raises(ContentParseError, match=reason):
        ADAPTER.parse(text)


def test_a_tree_that_makes_a_key_both_a_value_and_a_dict_is_refused() -> None:
    # given
    tree = Tree.create({("a",): Scalar("int", 1), ("a", "b"): Scalar("int", 2)})

    # when & then
    with pytest.raises(ContentParseError):
        ADAPTER.render(tree, "")


def test_a_tree_with_a_value_plist_cannot_hold_is_refused() -> None:
    # given
    tree = Tree.create({("b",): Scalar("date", "0000-00-00T00:00:00Z")})

    # when & then
    with pytest.raises(ContentParseError):
        ADAPTER.render(tree, "")


@pytest.mark.parametrize(
    "template",
    ["", "<plist/>", _xml({"a": 1}), _xml({"a": 1, "b": {"c": 2}, "z": [1, 2]}).replace("\n", "\r\n")],
    ids=["empty", "junk", "older", "crlf"],
)
def test_the_edits_turn_the_template_into_the_render(template: str) -> None:
    # given
    tree = ADAPTER.parse(_fixture())

    # when
    edits = ADAPTER.render_edits(tree, template)

    # then
    assert apply_edits(template, list(edits)) == ADAPTER.render(tree, template)


def test_a_one_key_change_is_one_small_edit() -> None:
    # given
    before = canonical_plist(_fixture().encode()).decode()
    changed = before.replace("<integer>4</integer>", "<integer>5</integer>")

    # when
    edits = ADAPTER.render_edits(ADAPTER.parse(changed), before)

    # then
    assert [(before[e.start : e.end], e.text) for e in edits] == [
        ("\t<integer>4</integer>\n", "\t<integer>5</integer>\n")
    ]


def test_a_template_that_is_already_canonical_needs_no_edit() -> None:
    # given
    text = canonical_plist(_fixture().encode()).decode()

    # when
    edits = ADAPTER.render_edits(ADAPTER.parse(text), text)

    # then
    assert edits == ()


def test_a_one_sided_change_merges_to_the_changed_bytes() -> None:
    # given
    base = _xml({"a": 1, "b": 2})
    local = _xml({"a": 9, "b": 2})

    # when
    merged = _merge(base, local, base)

    # then
    assert (merged.conflicted, merged.data.decode()) == (False, local)


def test_a_both_sides_change_to_one_key_is_a_conflict_and_to_two_keys_is_not() -> None:
    # given
    base = _xml({"a": 1, "b": 2})

    # when
    same_key = _merge(base, _xml({"a": 8, "b": 2}), _xml({"a": 9, "b": 2}))
    two_keys = _merge(base, _xml({"a": 8, "b": 2}), _xml({"a": 1, "b": 9}))

    # then
    assert (same_key.conflicted, two_keys.conflicted, two_keys.data.decode()) == (
        True,
        False,
        _xml({"a": 8, "b": 9}),
    )


def test_a_data_value_is_not_an_array_element_identity() -> None:
    # given
    rule = ArrayRule.create(identity=("Guid",), required=("Guid",), group=("Guid",))
    as_data = Object((("Guid", Scalar("data", "AQIDBA==")),))
    as_date = Object((("Guid", Scalar("date", "2024-01-02T03:04:05Z")),))
    as_str = Object((("Guid", Scalar("str", "AQIDBA==")),))

    # when
    identities = [element_identity(node, rule) for node in (as_data, as_date, as_str)]

    # then
    assert identities == [
        None,
        None,
        (("Guid", "AQIDBA=="),),
    ]


def test_importing_the_cli_does_not_import_the_plist_adapter() -> None:
    # given — a fresh interpreter, so modules other tests imported are absent
    code = "import sys, syncintosh.cli.main; print('syncintosh.adapters.plist' in sys.modules)"

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then
    assert result.stdout.strip() == "False"


# XML 1.0 cannot carry most control characters, so the text strategy leaves them out; a plist is UTF-8.
_text = st.text(st.characters(blacklist_categories=("Cs", "Cc")), max_size=8)
_dates = st.datetimes(min_value=datetime(1950, 1, 1), max_value=datetime(2100, 1, 1)).map(
    lambda value: value.replace(microsecond=0)
)
_scalars = (
    st.booleans()
    | st.integers(min_value=-(2**62), max_value=2**62)
    | st.floats(allow_nan=False, allow_infinity=False)
    | _text
    | st.binary(max_size=6)
    | _dates
)
_values = st.recursive(
    _scalars,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_text, children, max_size=3),
    max_leaves=8,
)
_documents = st.dictionaries(_text, _values, max_size=4)


@settings(deadline=None)
@given(document=_documents)
def test_parse_and_render_round_trip_every_document(document) -> None:
    # given
    text = _xml(document)

    # when
    tree = ADAPTER.parse(text)

    # then
    assert (ADAPTER.parse(ADAPTER.render(tree, text)), ADAPTER.render(tree, text)) == (
        tree,
        canonical_plist(text.encode()).decode(),
    )


@settings(deadline=None)
@given(document=_documents)
def test_merging_unchanged_input_is_idempotent(document) -> None:
    # given
    tree = ADAPTER.parse(_xml(document))

    # when
    result = merge_tree(tree, tree, tree)

    # then
    assert (result.merged, result.conflicts) == (tree, ())


@settings(deadline=None)
@given(base=_documents, changed=_documents)
def test_a_one_sided_change_always_wins(base, changed) -> None:
    # given
    base_text, changed_text = _xml(base), _xml(changed)

    # when
    from_local = _merge(base_text, changed_text, base_text)
    from_remote = _merge(base_text, base_text, changed_text)

    # then
    assert (from_local.conflicted, from_local.data.decode(), from_remote.conflicted, from_remote.data.decode()) == (
        False,
        changed_text,
        False,
        changed_text,
    )


@settings(deadline=None)
@given(base=_scalars, local=_scalars, remote=_scalars, other=_scalars)
def test_only_a_both_sides_change_to_the_same_key_conflicts(base, local, remote, other) -> None:
    # given — `moved` may change on both sides; `x` only on one side each, `y` on neither
    moved = [_xml({"moved": value, "other": other}) for value in (base, local, remote)]
    disjoint = [_xml({"x": x, "y": other}) for x in (base, local, base)]
    disjoint_remote = _xml({"x": base, "y": remote})
    trees = [ADAPTER.parse(text).value(("moved",)) for text in moved]

    # when
    both_sides = _merge(*moved)
    apart = _merge(disjoint[0], disjoint[1], disjoint_remote)

    # then
    assert (both_sides.conflicted, apart.conflicted) == (trees[1] != trees[2] and trees[0] not in trees[1:], False)
