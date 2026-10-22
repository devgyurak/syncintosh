# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for resolution choices injected into the merge engine (E2a).

The engine stays pure: a choice is a plain mapping from a conflict to the side an operator picked.
These tests hold the properties that make a choice safe to inject — a choice never reaches a path
that did not conflict, settling everything to one side leaves nothing to decide, and a resolved
result is stable when it is merged again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.content_merge import merge_content
from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.merge import merge_tree
from syncintosh.core.models import TextMerge
from syncintosh.core.paths import collision_key
from syncintosh.core.record_merge import merge_records
from syncintosh.core.resolution import CollisionChoice, CollisionKey, RecordChoice, Side, digest_of, fingerprint
from syncintosh.core.tree import KeyPath, Node, tree_from_value

ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID2 = "00112233445566778899aabbccddeeff"
ID3 = "11223344556677889900aabbccddeeff"
SHA1 = "a" * 64
SHA2 = "b" * 64
SHA3 = "c" * 64

_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(), children, max_size=3),
)
_trees = _json_values.map(tree_from_value)


def _under(entries: Mapping[KeyPath, Node], path: KeyPath) -> dict[KeyPath, Node]:
    """The entries at or beneath `path`: what a settled conflict must now show from its chosen side."""
    return {key: value for key, value in entries.items() if key[: len(path)] == path}


@given(base=_trees, local=_trees, remote=_trees, data=st.data())
def test_a_choice_on_a_path_that_does_not_conflict_is_ignored(base, local, remote, data) -> None:
    # given
    plain = merge_tree(base, local, remote)
    conflicted = {conflict.path for conflict in plain.conflicts}
    paths = sorted({*base.entries, *local.entries, *remote.entries})
    sides: list[Side] = data.draw(
        st.lists(st.sampled_from(["local", "remote"]), min_size=len(paths), max_size=len(paths))
    )
    # when
    picks = {path: side for path, side in zip(paths, sides, strict=True) if path not in conflicted}
    chosen = merge_tree(base, local, remote, choices=picks)
    # then
    assert chosen == plain


@given(base=_trees, local=_trees, remote=_trees)
def test_settling_every_conflict_to_local_reproduces_the_default_merge(base, local, remote) -> None:
    # given
    plain = merge_tree(base, local, remote)
    picks: dict[KeyPath, Side] = {conflict.path: "local" for conflict in plain.conflicts}
    # when
    chosen = merge_tree(base, local, remote, choices=picks)
    # then
    assert chosen.conflicts == ()
    assert chosen.merged == plain.merged


@given(base=_trees, local=_trees, remote=_trees)
def test_settling_every_conflict_to_remote_takes_the_remote_side(base, local, remote) -> None:
    # given
    plain = merge_tree(base, local, remote)
    picks: dict[KeyPath, Side] = {conflict.path: "remote" for conflict in plain.conflicts}
    # when
    chosen = merge_tree(base, local, remote, choices=picks)
    # then
    mismatched = [
        conflict.path
        for conflict in plain.conflicts
        if _under(chosen.merged.entries, conflict.path) != _under(remote.entries, conflict.path)
    ]
    assert chosen.conflicts == ()
    assert mismatched == []


@given(base=_trees, local=_trees, remote=_trees)
def test_a_resolved_merge_is_stable_when_merged_again(base, local, remote) -> None:
    # given
    plain = merge_tree(base, local, remote)
    picks: dict[KeyPath, Side] = {conflict.path: "remote" for conflict in plain.conflicts}
    resolved = merge_tree(base, local, remote, choices=picks).merged
    # when
    again = merge_tree(resolved, resolved, resolved)
    # then
    assert again.merged == resolved
    assert again.conflicts == ()


def test_a_remote_choice_on_a_structural_conflict_takes_the_remote_scalar() -> None:
    # given
    base = tree_from_value({"a": {"b": 1}})
    local = tree_from_value({"a": {"b": 2}})
    remote = tree_from_value({"a": 3})
    # when
    chosen = merge_tree(base, local, remote, choices={("a",): "remote"})
    kept = merge_tree(base, local, remote, choices={("a",): "local"})
    # then
    assert chosen.conflicts == ()
    assert chosen.merged == tree_from_value({"a": 3})
    assert kept.merged == tree_from_value({"a": {"b": 2}})


def test_an_ignored_local_key_survives_a_remote_choice_on_its_parent() -> None:
    # given
    base = tree_from_value({"a": {"keep": 1, "b": 1}})
    local = tree_from_value({"a": {"keep": 2, "b": 2}})
    remote = tree_from_value({"a": 3})
    # when
    chosen = merge_tree(base, local, remote, ignore=frozenset({("a", "keep")}), choices={("a",): "remote"})
    # then
    assert chosen.conflicts == ()
    assert chosen.merged == tree_from_value({"a": {"keep": 2}})


def test_a_choice_for_a_path_that_is_not_in_conflict_does_not_change_the_merge() -> None:
    # given
    base = tree_from_value({"a": 1, "b": 1})
    local = tree_from_value({"a": 2, "b": 1})
    remote = tree_from_value({"a": 1, "b": 3})
    # when
    chosen = merge_tree(base, local, remote, choices={("a",): "remote", ("b",): "local"})
    # then
    assert chosen.conflicts == ()
    assert chosen.merged == tree_from_value({"a": 2, "b": 3})


ID_KEYS = [ID1, ID2, ID3]
PATHS = [".zshrc", ".ZSHRC"]
SHAS = [SHA1, SHA2, SHA3]


def _rec(file_id: str, sha: str, path: str) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=file_id, sha256=sha, path=path, preset="zsh", scope="common", machine=None, format="text"
    )


_record_maps = st.dictionaries(
    st.sampled_from(ID_KEYS), st.tuples(st.sampled_from(SHAS), st.sampled_from(PATHS)), max_size=3
).map(lambda entries: {file_id: _rec(file_id, sha, path) for file_id, (sha, path) in entries.items()})
_bases = st.none() | _record_maps


def _record_pick(kind: str, side: Side) -> RecordChoice:
    """The choice an operator makes on a record conflict to settle it to `side`."""
    return "keep" if kind == "delete/modify" else side


@given(base=_bases, local=_record_maps, remote=_record_maps)
def test_settling_every_record_conflict_to_local_leaves_nothing_to_decide(base, local, remote) -> None:
    # given
    first = merge_records(base, local, remote)
    picks: dict[str, RecordChoice] = {
        conflict.file_id: _record_pick(conflict.kind, "local") for conflict in first.conflicts
    }
    settled = merge_records(base, local, remote, choices=picks)
    collisions: dict[CollisionKey, CollisionChoice] = {
        (c.scope, c.machine, collision_key(c.path)): "local" for c in settled.collisions
    }
    # when
    chosen = merge_records(base, local, remote, choices=picks, collision_choices=collisions)
    # then
    assert chosen.conflicts == ()
    assert chosen.collisions == ()


@given(base=_bases, local=_record_maps, remote=_record_maps)
def test_settling_every_record_conflict_to_remote_takes_the_remote_record(base, local, remote) -> None:
    # given
    first = merge_records(base, local, remote)
    picks: dict[str, RecordChoice] = {
        conflict.file_id: _record_pick(conflict.kind, "remote") for conflict in first.conflicts
    }
    # when
    chosen = merge_records(base, local, remote, choices=picks)
    # then
    mismatched = [
        conflict.file_id
        for conflict in first.conflicts
        if conflict.kind != "delete/modify"
        and conflict.file_id not in chosen.deduplicated
        and chosen.merged.get(conflict.file_id) != conflict.remote
    ]
    assert chosen.conflicts == ()
    assert mismatched == []


@given(base=_bases, local=_record_maps, remote=_record_maps)
def test_a_record_choice_on_a_non_conflicting_file_is_ignored(base, local, remote) -> None:
    # given
    plain = merge_records(base, local, remote)
    conflicted = {conflict.file_id for conflict in plain.conflicts}
    picks: dict[str, RecordChoice] = {file_id: "local" for file_id in {*local, *remote} if file_id not in conflicted}
    # when
    chosen = merge_records(base, local, remote, choices=picks)
    # then
    assert chosen == plain


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("local", [ID1]), ("remote", [ID2]), ("drop", [])],
)
def test_a_path_collision_keeps_the_chosen_sides_file_id(choice: CollisionChoice, expected: list[str]) -> None:
    # given
    local = {ID1: _rec(ID1, SHA1, ".zshrc")}
    remote = {ID2: _rec(ID2, SHA2, ".zshrc")}
    # when
    result = merge_records(None, local, remote, collision_choices={("common", None, ".zshrc"): choice})
    # then
    assert sorted(result.merged) == expected
    assert result.collisions == ()


@pytest.mark.parametrize(("choice", "kept", "deleted"), [("keep", [ID1], []), ("delete", [], [ID1])])
def test_a_delete_modify_keeps_or_deletes_the_surviving_record(
    choice: RecordChoice, kept: list[str], deleted: list[str]
) -> None:
    # given
    base = {ID1: _rec(ID1, SHA1, ".zshrc")}
    local: dict[str, ManifestRecord] = {}
    remote = {ID1: _rec(ID1, SHA2, ".zshrc")}
    # when
    result = merge_records(base, local, remote, choices={ID1: choice})
    # then
    assert sorted(result.merged) == kept
    assert list(result.deleted) == deleted
    assert result.conflicts == ()


def test_a_record_choice_that_does_not_fit_the_conflict_kind_is_rejected() -> None:
    # given
    local = {ID1: _rec(ID1, SHA1, ".zshrc")}
    remote = {ID1: _rec(ID1, SHA2, ".zshrc")}
    # when & then
    with pytest.raises(ValueError):
        merge_records(None, local, remote, choices={ID1: "keep"})


@dataclass
class _RecordingDriver:
    """A line-level driver that records each call, so a test can prove it was never reached."""

    calls: list[tuple[str, str, str]]

    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        self.calls.append((base, local, remote))
        return TextMerge(text="unused", conflicted=False)


def test_a_file_choice_takes_the_chosen_side_and_skips_the_driver() -> None:
    # given
    driver = _RecordingDriver(calls=[])
    # when
    result = merge_content(b"base\n", b"local\n", b"remote\n", driver, choice="remote")
    # then
    assert result.data == b"remote\n"
    assert result.conflicted is False
    assert driver.calls == []


def test_a_key_choice_settles_only_that_key_in_a_structural_merge() -> None:
    # given
    base = b'{"a": 1, "b": 1}'
    local = b'{"a": 2, "b": 1}'
    remote = b'{"a": 3, "b": 2}'
    # when
    result = merge_content(
        base, local, remote, _RecordingDriver(calls=[]), JsoncAdapter(), key_choices={("a",): "remote"}
    )
    # then
    assert result.conflicted is False
    assert JsoncAdapter().parse(result.data.decode("utf-8")) == tree_from_value({"a": 3, "b": 2})


@pytest.mark.parametrize("side", ["base", "local", "remote"])
def test_a_fingerprint_changes_when_any_side_changes(side: str) -> None:
    # given
    sides = {"base": digest_of("b"), "local": digest_of("l"), "remote": digest_of("r")}
    changed = {**sides, side: digest_of("changed")}
    # when
    original = fingerprint("file", ("a",), sides["base"], sides["local"], sides["remote"])
    moved = fingerprint("file", ("a",), changed["base"], changed["local"], changed["remote"])
    # then
    assert moved != original


def test_a_fingerprint_never_contains_the_plaintext_value() -> None:
    # given
    secret = "hunter2-do-not-print"
    # when
    value = fingerprint("file", ("token",), digest_of(secret), digest_of("x"), digest_of("y"))
    # then
    assert secret not in value
    assert digest_of(secret) != secret
