# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for carrying a user-ignored key through projection.

A carried key is not stripped: its sealed value comes from an object the repo already holds, and
the live value is put back on apply (``docs/design/02-base-and-merge.md``). The properties run the
real JSONC adapter over generated documents (``AGENTS.md`` §5.3).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.content_merge import merge_content
from syncintosh.core.errors import ProjectionError
from syncintosh.core.ignore import IGNORED_SETTINGS_KEY, IgnoreSpec
from syncintosh.core.ledger import classify
from syncintosh.core.models import LedgerEntry, TextMerge
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import NO_CARRY, Carry, carry, project_text, restore_text
from syncintosh.core.tree import tree_from_value

ADAPTER = JsoncAdapter()


def _policy(*, noise=(), excluded=()) -> PresetFile:
    return PresetFile.create(path="settings.json", format="jsonc", noise=noise, excluded=excluded)


def _doc(**members: object) -> str:
    return json.dumps(members, indent=2) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _NoDriver:
    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        raise AssertionError("a structural merge never reaches the line driver")


CARRY_K = frozenset({("k",)})


# --------------------------------------------------------------------------- carry()


def test_carry_swaps_the_value_in_place() -> None:
    # given
    tree = tree_from_value({"a": 1, "k": 2, "z": 3})
    source = tree_from_value({"k": 99})

    # when
    result = carry(tree, CARRY_K, source)

    # then — the key keeps its position, so the renderer edits it where it stands
    assert list(result) == [("a",), ("k",), ("z",)]
    assert result.value(("k",)) == source.value(("k",))


def test_carry_appends_a_key_the_tree_does_not_have() -> None:
    # when
    result = carry(tree_from_value({"a": 1}), CARRY_K, tree_from_value({"k": 5}))

    # then
    assert result.to_value() == {"a": 1, "k": 5}


@pytest.mark.parametrize("source", [None, tree_from_value({"other": 1})])
def test_carry_drops_a_key_the_source_does_not_hold(source) -> None:
    # when
    result = carry(tree_from_value({"a": 1, "k": 2}), CARRY_K, source)

    # then
    assert result.to_value() == {"a": 1}


def test_carry_replaces_a_whole_object_not_a_merge_of_both() -> None:
    # when
    result = carry(tree_from_value({"k": {"x": 1, "y": 2}}), CARRY_K, tree_from_value({"k": {"z": 3}}))

    # then
    assert result.to_value() == {"k": {"z": 3}}


def test_carry_with_no_paths_returns_the_same_tree() -> None:
    # given
    tree = tree_from_value({"a": 1})

    # when & then
    assert carry(tree, frozenset(), tree_from_value({"a": 2})) is tree


# --------------------------------------------------------------------------- project_text


def test_a_carried_key_is_sealed_with_the_sources_value_not_the_live_one() -> None:
    # given
    live = _doc(a=1, k="LIVE")
    source = _doc(a=0, k="SOURCE")

    # when
    sealed = project_text(ADAPTER, live, _policy(), Carry(CARRY_K, source))

    # then
    assert json.loads(sealed) == {"a": 1, "k": "SOURCE"}


def test_a_carried_key_without_a_source_is_left_out_of_the_seal() -> None:
    # when
    sealed = project_text(ADAPTER, _doc(a=1, k="LIVE"), _policy(), Carry(CARRY_K, None))

    # then
    assert json.loads(sealed) == {"a": 1}


def test_a_carried_key_the_live_file_lacks_is_added_from_the_source() -> None:
    # when
    sealed = project_text(ADAPTER, _doc(a=1), _policy(), Carry(CARRY_K, _doc(k="SOURCE")))

    # then
    assert json.loads(sealed) == {"a": 1, "k": "SOURCE"}


def test_a_carry_keeps_the_comments_of_the_live_file() -> None:
    # given
    live = '{\n  // keep me\n  "a": 1,\n  "k": "LIVE"\n}\n'

    # when
    sealed = project_text(ADAPTER, live, _policy(), Carry(CARRY_K, _doc(k="SOURCE")))

    # then
    assert sealed == '{\n  // keep me\n  "a": 1,\n  "k": "SOURCE"\n}\n'


def test_a_carried_value_that_holds_an_excluded_key_is_not_sealed() -> None:
    # given — the source object predates the exclusion, or came from a build that did not have it
    policy = _policy(excluded={("k", "token")})
    source = _doc(k={"token": "LEAK", "keep": 1})

    # when
    sealed = project_text(ADAPTER, _doc(a=1), policy, Carry(CARRY_K, source))

    # then
    assert json.loads(sealed) == {"a": 1, "k": {"keep": 1}}


def test_a_carry_that_changes_nothing_returns_the_text_untouched() -> None:
    # given
    live = '{"k": 1, "a": 2}  \n'

    # when & then
    assert project_text(ADAPTER, live, _policy(), Carry(CARRY_K, '{"k": 1}')) == live


def test_a_carry_source_that_is_not_the_format_is_refused() -> None:
    # when & then
    with pytest.raises(ProjectionError):
        project_text(ADAPTER, _doc(a=1), _policy(), Carry(CARRY_K, "{not json"))


# --------------------------------------------------------------------------- restore_text


def test_restore_puts_the_live_value_back_over_the_carried_one() -> None:
    # given — the repo holds the source's value; this machine keeps its own
    repo = _doc(a=2, k="SOURCE")
    live = _doc(a=1, k="LIVE")

    # when
    restored = restore_text(ADAPTER, repo, live, _policy(), Carry(CARRY_K, repo))

    # then
    assert json.loads(restored) == {"a": 2, "k": "LIVE"}


def test_restore_does_not_write_the_remote_value_where_the_machine_has_none() -> None:
    # given — this machine never set `k`, and the repo carries another machine's value
    repo = _doc(a=2, k="REMOTE")

    # when
    restored = restore_text(ADAPTER, repo, _doc(a=1), _policy(), Carry(CARRY_K, repo))

    # then
    assert json.loads(restored) == {"a": 2}


def test_restore_checks_the_round_trip_against_the_object_being_applied() -> None:
    # given — a stale collect-time source: the object being applied is what the next cycle carries from
    repo = _doc(a=2, k="NEW")
    stale_source = _doc(k="OLD")
    live = _doc(a=1, k="LIVE")

    # when
    restored = restore_text(ADAPTER, repo, live, _policy(), Carry(CARRY_K, stale_source))

    # then
    assert project_text(ADAPTER, restored, _policy(), Carry(CARRY_K, repo)) == repo


def test_restore_still_re_attaches_a_noise_key_beside_a_carried_one() -> None:
    # given
    policy = _policy(noise={("noise",)})
    repo = _doc(a=2, k="SOURCE")
    live = _doc(a=1, k="LIVE", noise=9)

    # when
    restored = restore_text(ADAPTER, repo, live, policy, Carry(CARRY_K, repo))

    # then
    assert json.loads(restored) == {"a": 2, "k": "LIVE", "noise": 9}


def test_restore_writes_a_carried_value_as_the_live_file_wrote_it_and_a_noise_value_the_ordinary_way() -> None:
    # given — both keys hold a number the live file spells in a non-canonical way
    policy = _policy(noise={("noise",)})
    repo = '{\n  "a": 2\n}\n'
    live = '{\n  "a": 1,\n  "k": 1e3,\n  "noise": 2.50\n}\n'

    # when
    restored = restore_text(ADAPTER, repo, live, policy, Carry(CARRY_K, repo))

    # then — `1e3` is copied; `2.50` is re-attached by `json.dumps`, which is what develop did for noise
    assert restored == '{\n  "a": 2,\n  "k": 1e3,\n  "noise": 2.5\n}\n'


# --------------------------------------------------------------------------- the carry source


def test_the_baseline_object_is_the_carry_source_because_change_detection_reads_the_ledger() -> None:
    # given — the remote moved the ignored key from 1 to 2 while this machine changed its own to 99
    baseline = _doc(a=1, k=1)
    head = _doc(a=1, k=2)
    live = _doc(a=1, k=99)
    entry = LedgerEntry(sha256=_sha(baseline), commit="c0", suppressed=False)
    paths = frozenset({("k",)})

    # when
    from_baseline = project_text(ADAPTER, live, _policy(), Carry(paths, baseline))
    from_head = project_text(ADAPTER, live, _policy(), Carry(paths, head))

    # then — only the baseline leaves this machine with no local change, the remote change waiting to be applied
    assert classify(entry, _sha(from_baseline), _sha(head)) == "pending-apply"
    assert classify(entry, _sha(from_head), _sha(head)) == "local-change"


def test_a_stale_base_with_a_remote_change_to_the_ignored_key_merges_without_a_conflict() -> None:
    # given — local edited `a`; the remote (HEAD) moved only the ignored key past the baseline
    baseline = _doc(a=1, k=1)
    head = _doc(a=1, k=2)
    live = _doc(a=5, k=99)
    local = project_text(ADAPTER, live, _policy(), Carry(frozenset({("k",)}), baseline))

    # when
    merged = merge_content(baseline.encode(), local.encode(), head.encode(), _NoDriver(), ADAPTER)

    # then — both changes land, and nothing asks the user
    assert (merged.conflicted, json.loads(merged.data)) == (False, {"a": 5, "k": 2})


# --------------------------------------------------------------------------- the spec feeds it


def test_a_spec_read_from_settings_yields_the_paths_a_carry_takes() -> None:
    # given
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["k"]})
    live = _doc(a=1, k="LIVE")

    # when
    sealed = project_text(ADAPTER, live, _policy(), Carry(spec.paths_for(_policy()), _doc(k="SOURCE")))

    # then
    assert json.loads(sealed) == {"a": 1, "k": "SOURCE"}


# --------------------------------------------------------------------------- properties

_LEAVES = st.none() | st.booleans() | st.integers() | st.text(max_size=6)
_OBJECTS = st.dictionaries(st.text(min_size=1, max_size=6), _LEAVES, max_size=4)
_INDENT = st.sampled_from([None, 2])
_POLICY = _policy(noise={("noise",)}, excluded={("secret",)})
_CARRIED = frozenset({("k",)})


def _document(value: dict, k: object, indent: int | None, **extra: object) -> str:
    return json.dumps({**value, "k": k, **extra}, indent=indent, ensure_ascii=False)


@given(value=_OBJECTS, before=_LEAVES, after=_LEAVES, source=_LEAVES, indent=_INDENT)
def test_a_change_to_only_a_carried_key_does_not_change_the_projection(value, before, after, source, indent) -> None:
    # given
    sealed_from = _document({}, source, indent)

    # when
    first = project_text(ADAPTER, _document(value, before, indent), _POLICY, Carry(_CARRIED, sealed_from))
    second = project_text(ADAPTER, _document(value, after, indent), _POLICY, Carry(_CARRIED, sealed_from))

    # then
    assert first == second


@given(value=_OBJECTS, live_k=_LEAVES, source=_LEAVES, local_noise=st.integers(), indent=_INDENT)
def test_project_of_restore_is_the_repo_object(value, live_k, source, local_noise, indent) -> None:
    # given — the repo object is a projection of some file; this machine's file differs in every local key
    carried = Carry(_CARRIED, _document({}, source, indent))
    repo = project_text(ADAPTER, _document(value, "SEALED", indent, noise=1, secret="X"), _POLICY, carried)
    live = _document(value, live_k, indent, noise=local_noise, secret="LOCAL")

    # when
    restored = restore_text(ADAPTER, repo, live, _POLICY, carried)

    # then
    assert project_text(ADAPTER, restored, _POLICY, Carry(_CARRIED, repo)) == repo


@given(value=_OBJECTS, k=_LEAVES, source=_LEAVES, indent=_INDENT)
def test_a_projection_with_a_carry_is_idempotent(value, k, source, indent) -> None:
    # given
    carried = Carry(_CARRIED, _document({}, source, indent))

    # when
    once = project_text(ADAPTER, _document(value, k, indent, noise=1, secret="X"), _POLICY, carried)
    twice = project_text(ADAPTER, once, _POLICY, carried)

    # then
    assert twice == once


_LISTED = st.lists(st.sampled_from(["k", "secret", "noise", "other", "-k"]), max_size=5)


@given(listed=_LISTED, value=_OBJECTS, indent=_INDENT)
def test_an_excluded_key_never_reaches_the_output_whatever_the_list_says(listed, value, indent) -> None:
    # given — the source object holds the excluded keys, and the list may name them
    policy = _policy(noise={("noise",)}, excluded={("secret",), ("k", "token")})
    source = json.dumps({"k": {"token": "LEAK", "v": 1}, "secret": "LEAK", "noise": "LEAK", "other": "LEAK"})
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: listed})
    live = _document(value, {"token": "LEAK", "v": 2}, indent, secret="LEAK", noise="LEAK")

    # when
    sealed = project_text(ADAPTER, live, policy, Carry(spec.paths_for(policy), source))

    # then
    paths = list(ADAPTER.parse(sealed))
    assert [path for path in paths if path[:1] in (("secret",), ("noise",)) or path[:2] == ("k", "token")] == []


def test_the_default_carry_is_inert() -> None:
    # when & then
    assert (NO_CARRY.paths, NO_CARRY.source) == (frozenset(), None)
