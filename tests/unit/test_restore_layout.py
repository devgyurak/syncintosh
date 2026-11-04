# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for restoring a stripped key where the live file had it (issue #27).

`restore_text` runs the live file's projection edits backwards onto the repo text, so a re-attached key keeps its
position, its trailing comment, and its one-line layout. When a line beside the edit changed on the other machine it
falls back to the renderer, which gets the values right and the layout merely tidy.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.ports import TextEdit
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import Carry, _place, project_text, restore_text

ADAPTER = JsoncAdapter()
NOISE_TOP = ("noise",)
NOISE_NESTED = ("win", "noise")
CARRIED = frozenset({("k",)})

# One document per way the old renderer disturbed a key it put back (issue #27), plus the edges of the file.
LAYOUTS = {
    "key moved to the end of its object": ('{\n  "win": {"x": 1, "noise": 2, "y": 3},\n  "b": 1\n}\n', [NOISE_NESTED]),
    "parent object stripped whole": ('{\n  "a": 1,\n  "win": {"noise": 2},\n  "b": 1\n}\n', [NOISE_NESTED]),
    "trailing comment": ('{\n  "a": 1,\n  "noise": 2, // why\n  "b": 1\n}\n', [NOISE_TOP]),
    "comment above the key": (
        '{\n  "a": 1,\n  // about noise\n  "noise": 2,\n  // about b\n  "b": 1\n}\n',
        [NOISE_TOP],
    ),
    "nested value written by hand": (
        '{\n  "a": 1,\n  "noise": {"deep": {"k": 1,  "j": [1,2]}},\n  "b": 1\n}\n',
        [NOISE_TOP],
    ),
    "last member of the file": ('{\n  "a": 1,\n  "b": 1,\n  "noise": 2\n}\n', [NOISE_TOP]),
    "first member of the file": ('{\n  "noise": 2,\n  "a": 1\n}\n', [NOISE_TOP]),
    "windows newlines": ('{\r\n  "a": 1,\r\n  "noise": 2, // why\r\n  "b": 1\r\n}\r\n', [NOISE_TOP]),
    "two keys on one line": (
        '{\n  "a": 1,\n  "noise": 2, "win": {"noise": 3},\n  "b": 1\n}\n',
        [NOISE_TOP, NOISE_NESTED],
    ),
}


def _policy(noise) -> PresetFile:
    return PresetFile.create(path="settings.json", format="jsonc", noise=noise)


@pytest.mark.parametrize(("live", "noise"), list(LAYOUTS.values()), ids=list(LAYOUTS))
def test_restoring_an_unchanged_projection_gives_back_the_live_file(live, noise) -> None:
    # given
    policy = _policy(noise)
    repo = project_text(ADAPTER, live, policy)

    # when
    restored = restore_text(ADAPTER, repo, live, policy)

    # then — nothing changed on the other machine, so nothing about the file changes here either
    assert restored == live


@pytest.mark.parametrize(
    ("old", "new"),
    [('"p1": 1', '"p1": 100'), ('"z": 9', '"z": 90')],
    ids=["far above", "far below"],
)
def test_a_line_the_other_machine_changed_far_away_comes_across_and_the_local_lines_stay(old, new) -> None:
    # given — the local keys sit mid-file, with untouched lines on both sides of them
    spacer = '  "p1": 1,\n  "p2": 2,\n  "p3": 3,\n'
    live = (
        "{\n"
        + spacer
        + '  "noise": 2, // why\n  "win": {"x": 1, "noise": 4, "y": 3},\n'
        + spacer.replace("p", "q")
        + '  "z": 9\n}\n'
    )
    policy = _policy([NOISE_TOP, NOISE_NESTED])
    repo = project_text(ADAPTER, live, policy).replace(old, new)

    # when
    restored = restore_text(ADAPTER, repo, live, policy)

    # then
    assert restored == live.replace(old, new)


def test_a_line_the_other_machine_changed_beside_the_key_falls_back_to_a_render_with_the_right_values() -> None:
    # given — the line right above the stripped key changed, so where the key belongs is not known
    live = '{\n  "p1": 1,\n  "p3": 3,\n  "noise": 2, // why\n  "z": 9\n}\n'
    policy = _policy([NOISE_TOP])
    repo = project_text(ADAPTER, live, policy).replace('"p3": 3', '"p3": 30')

    # when
    restored = restore_text(ADAPTER, repo, live, policy)

    # then — exactly what the renderer writes: the key by its sibling, and no comment, which is the limit
    # the design doc names. A looser neighbour rule would keep `// why` here and fail this.
    assert restored == '{\n  "p1": 1,\n  "p3": 30,\n  "noise": 2,\n  "z": 9\n}\n'
    assert project_text(ADAPTER, restored, policy) == repo


def test_an_edit_at_the_end_of_the_text_is_placed_at_the_end_of_the_repo_text() -> None:
    # given — an insertion after the last line, into a repo text that ends the same way
    shown = '{\n  "a": 1\n}\n'

    # when
    placed = _place(shown, shown, [TextEdit(len(shown), len(shown), "X")])

    # then
    assert placed == [TextEdit(len(shown), len(shown), "X")]


def test_an_edit_at_the_end_of_the_text_is_not_placed_when_the_repo_text_goes_on() -> None:
    # given — the other machine appended a line after where the edit stood
    shown = '{\n  "a": 1\n}\n'

    # when
    placed = _place(shown, shown + "// more\n", [TextEdit(len(shown), len(shown), "X")])

    # then
    assert placed is None


def test_a_carried_key_and_a_noise_key_each_come_back_as_the_live_file_wrote_them() -> None:
    # given — `k` is carried from the repo object, `noise` is stripped, and both stand on lines of their own
    live = '{\n  "a": 1,\n  "k": 1e3, // mine\n  "noise": 2, // why\n  "b": 1\n}\n'
    policy = _policy([NOISE_TOP])
    carried = Carry(CARRIED, '{\n  "k": 5\n}\n')
    repo = project_text(ADAPTER, live, policy, carried)

    # when
    restored = restore_text(ADAPTER, repo, live, policy, carried)

    # then
    assert restored == live


_LEAVES = st.none() | st.booleans() | st.integers() | st.text(max_size=6)
_OBJECTS = st.dictionaries(st.text(min_size=1, max_size=6), _LEAVES, max_size=4)
_INDENT = st.sampled_from([None, 2])
_STRIPPED = _policy([NOISE_TOP, NOISE_NESTED])


def _document(value: dict, indent: int | None, **extra: object) -> str:
    return json.dumps({**value, **extra}, indent=indent, ensure_ascii=False)


@given(value=_OBJECTS, noise=_LEAVES, nested=_LEAVES, indent=_INDENT)
def test_restoring_a_files_own_projection_is_the_identity(value, noise, nested, indent) -> None:
    # given
    live = _document(value, indent, noise=noise, win={"noise": nested, "x": 1})
    repo = project_text(ADAPTER, live, _STRIPPED)

    # when
    restored = restore_text(ADAPTER, repo, live, _STRIPPED)

    # then
    assert restored == live


@given(value=_OBJECTS, live_k=_LEAVES, source_k=_LEAVES, noise=_LEAVES, indent=_INDENT)
def test_restoring_a_projection_made_with_a_carry_is_the_identity(value, live_k, source_k, noise, indent) -> None:
    # given
    carried = Carry(CARRIED, _document({}, indent, k=source_k))
    live = _document(value, indent, k=live_k, noise=noise)
    repo = project_text(ADAPTER, live, _STRIPPED, carried)

    # when
    restored = restore_text(ADAPTER, repo, live, _STRIPPED, carried)

    # then
    assert restored == live
