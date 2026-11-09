# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the allowlist projection (`PresetFile.synced`): only the named keys travel, the rest stay local.

The allowlist is the opposite of `noise`: a key the app adds later is local until the preset names it, so these
tests drive keys the policy has never heard of. The properties run over generated documents (``AGENTS.md`` §5.3).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ConfigError, ProjectionError
from syncintosh.core.ignore import IgnoreSpec
from syncintosh.core.policy import _file_description, policy_digest
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.projection import dropped_items, graft, project_text, restore_text, strip
from syncintosh.core.tree import ANY_KEY, Tree, tree_from_value

ADAPTER = JsoncAdapter()

SYNCED = frozenset({("theme",), ("editor",)})

SETTINGS = {
    "theme": "dark",
    "editor": {"fontSize": 13, "token": "FAKE-TOKEN"},
    "windowBounds": {"width": 800},
    "brandNewKey": True,
}


def _policy(*, synced=SYNCED, noise=(), excluded=()) -> PresetFile:
    return PresetFile.create(path="settings.json", format="jsonc", synced=synced, noise=noise, excluded=excluded)


def _text(value: dict) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _tree(value: dict) -> Tree:
    return tree_from_value(value)


def _loaded(text: str) -> dict:
    return json.loads(text)


def _preset(file: PresetFile) -> Preset:
    return Preset.create(name="demo", files=(file,))


def test_projection_keeps_only_the_allowlisted_keys() -> None:
    # when
    projected = project_text(ADAPTER, _text(SETTINGS), _policy())

    # then
    assert _loaded(projected) == {"theme": "dark", "editor": {"fontSize": 13, "token": "FAKE-TOKEN"}}


def test_a_key_the_app_adds_later_is_not_projected() -> None:
    # given
    later = {**SETTINGS, "addedInNextRelease": {"x": 1}}

    # when
    first = project_text(ADAPTER, _text(SETTINGS), _policy())
    second = project_text(ADAPTER, _text(later), _policy())

    # then
    assert first == second


def test_a_change_outside_the_allowlist_leaves_the_projection_alone() -> None:
    # given
    moved = {**SETTINGS, "windowBounds": {"width": 1200}}

    # when
    first = project_text(ADAPTER, _text(SETTINGS), _policy())
    second = project_text(ADAPTER, _text(moved), _policy())

    # then
    assert first == second


def test_excluded_inside_the_allowlist_is_still_removed() -> None:
    # when
    projected = project_text(ADAPTER, _text(SETTINGS), _policy(excluded={("editor", "token")}))

    # then
    assert _loaded(projected) == {"theme": "dark", "editor": {"fontSize": 13}}


def test_noise_inside_the_allowlist_is_still_local() -> None:
    # when
    projected = project_text(ADAPTER, _text(SETTINGS), _policy(noise={("editor", "fontSize")}))

    # then
    assert _loaded(projected) == {"theme": "dark", "editor": {"token": "FAKE-TOKEN"}}


def test_a_key_beneath_an_allowlisted_key_travels() -> None:
    # when
    projected = project_text(ADAPTER, _text(SETTINGS), _policy(synced={("editor", "fontSize")}))

    # then
    assert _loaded(projected) == {"editor": {"fontSize": 13}}


def test_a_file_with_nothing_allowlisted_projects_to_the_empty_object() -> None:
    # when
    projected = project_text(ADAPTER, _text({"windowBounds": {"width": 1}}), _policy())

    # then
    assert _loaded(projected) == {}


def test_restore_puts_back_the_local_values_outside_the_allowlist() -> None:
    # given — the repo holds only the shared keys; this machine's file has its own window and a new key
    repo_text = project_text(ADAPTER, _text({"theme": "light", "editor": {"fontSize": 15}}), _policy())

    # when
    restored = restore_text(ADAPTER, repo_text, _text(SETTINGS), _policy())

    # then
    assert _loaded(restored) == {
        "theme": "light",
        "editor": {"fontSize": 15},
        "windowBounds": {"width": 800},
        "brandNewKey": True,
    }


def test_restore_refuses_an_object_that_still_holds_a_key_outside_the_allowlist() -> None:
    # given — an object from before the allowlist existed: it is not a projection, so it must be migrated first
    repo_text = _text({"theme": "light", "windowBounds": {"width": 1}})

    # when & then
    with pytest.raises(ProjectionError, match="did not round-trip"):
        restore_text(ADAPTER, repo_text, _text(SETTINGS), _policy())


def test_restore_and_project_round_trip_to_the_repo_text() -> None:
    # given
    policy = _policy(excluded={("editor", "token")})
    repo_text = project_text(ADAPTER, _text(SETTINGS), policy)

    # when
    restored = restore_text(ADAPTER, repo_text, _text({**SETTINGS, "theme": "light"}), policy)

    # then
    assert project_text(ADAPTER, restored, policy) == repo_text


def test_strip_with_an_allowlist_keeps_nothing_else() -> None:
    # when
    kept = strip(_tree(SETTINGS), frozenset(), SYNCED)

    # then
    assert set(kept.entries) == {("theme",), ("editor", "fontSize"), ("editor", "token")}


def test_graft_with_an_allowlist_takes_outside_keys_from_live_and_the_rest_from_repo() -> None:
    # given — the repo deleted `editor`, which is shared, so it goes; the outside keys are live's
    repo = _tree({"theme": "light"})

    # when
    grafted = graft(repo, _tree(SETTINGS), frozenset(), frozenset(), SYNCED)

    # then
    assert dict(grafted.entries) == {
        ("theme",): repo.entries[("theme",)],
        **{path: node for path, node in _tree(SETTINGS).entries.items() if path[0] in {"windowBounds", "brandNewKey"}},
    }


def test_an_outside_key_never_counts_as_a_dropped_item() -> None:
    # given — the allowlist's local keys are not user-named items
    live = _tree({"servers": {"a": {"command": "x"}}, "windowBounds": {"width": 1}})

    # when
    dropped = dropped_items(_tree({"theme": "light"}), live, frozenset(), frozenset(), SYNCED)

    # then
    assert dropped == ()


@pytest.mark.parametrize(
    ("synced", "message"),
    [
        pytest.param(set(), "at least one key", id="empty"),
        pytest.param({()}, "below the root", id="root"),
        pytest.param({("auth",)}, "also excluded", id="equals-excluded"),
        pytest.param({("auth", "token")}, "also excluded", id="under-excluded"),
        pytest.param({("servers", "a", "env")}, "also excluded", id="under-wildcard-excluded"),
    ],
)
def test_an_unsafe_allowlist_is_refused(synced, message) -> None:
    # when & then
    with pytest.raises(ConfigError, match=message):
        PresetFile.create(
            path="settings.json",
            format="jsonc",
            synced=synced,
            excluded={("auth",), ("servers", ANY_KEY, "env")},
        )


@pytest.mark.parametrize(
    "synced",
    [
        pytest.param({(ANY_KEY,)}, id="all-wildcard"),
        pytest.param({("servers", ANY_KEY, "env")}, id="wildcard-segment"),
        pytest.param({("theme",), ("a", ANY_KEY)}, id="one-bad-among-good"),
    ],
)
def test_an_allowlist_with_a_wildcard_segment_is_refused(synced) -> None:
    # when & then
    with pytest.raises(ConfigError, match="literal keys only"):
        PresetFile.create(path="settings.json", format="jsonc", synced=synced)


def test_an_allowlist_with_an_ignore_list_is_refused() -> None:
    # when & then
    with pytest.raises(ConfigError, match="ignore list"):
        PresetFile.create(path="settings.json", format="jsonc", synced={("theme",)}, ignore_list=True)


@pytest.mark.parametrize("format", ["text", "binary"])
def test_an_allowlist_needs_a_keyed_format(format) -> None:
    # when & then
    with pytest.raises(ConfigError, match="keyed format"):
        PresetFile.create(path="settings.txt", format=format, synced={("theme",)})


def test_an_allowlist_may_name_a_key_above_an_excluded_one() -> None:
    # when
    policy = _policy(synced={("editor",)}, excluded={("editor", "token")})

    # then
    assert policy.synced == frozenset({("editor",)})


def test_no_allowlist_is_the_default_and_strips_nothing() -> None:
    # when
    policy = PresetFile.create(path="settings.json", format="jsonc")

    # then
    assert (policy.synced, policy.strips) == (None, False)


def test_a_policy_without_an_allowlist_keeps_its_digest_description() -> None:
    # when
    description = _file_description(PresetFile.create(path="settings.json", format="jsonc", noise={("a",)}))

    # then
    assert "synced" not in description


def test_the_digest_of_a_policy_without_an_allowlist_is_pinned() -> None:
    # given — recorded before the allowlist existed; a change here would strand every shipped repo
    file = PresetFile.create(path="settings.json", format="jsonc", noise={("a",)}, excluded={("b",)})

    # when
    digest = policy_digest(_preset(file))

    # then
    assert digest == "9496dee2f69546ca798b95be4ab2a5578a7f8bfbf02ad67966e7279f9478a458"


@pytest.mark.parametrize("other", [{("theme",)}, {("theme",), ("editor",)}])
def test_the_allowlist_is_part_of_the_digest(other) -> None:
    # when
    plain = policy_digest(_preset(PresetFile.create(path="settings.json", format="jsonc")))
    listed = policy_digest(_preset(PresetFile.create(path="settings.json", format="jsonc", synced=other)))

    # then
    assert plain != listed


def test_two_allowlists_of_the_same_keys_share_a_digest_whatever_the_order() -> None:
    # when
    first = policy_digest(_preset(_policy(synced=[("a",), ("b",)])))
    second = policy_digest(_preset(_policy(synced=[("b",), ("a",)])))

    # then
    assert first == second


@pytest.mark.parametrize(
    ("keys", "carried"),
    [
        pytest.param({"theme", "windowBounds"}, {("theme",)}, id="only-the-allowlisted-key"),
        pytest.param({"windowBounds"}, set(), id="outside-key-is-already-local"),
    ],
)
def test_an_ignored_key_outside_the_allowlist_is_not_carried(keys, carried) -> None:
    # given
    spec = IgnoreSpec(keys=frozenset(keys))

    # when
    paths = spec.paths_for(_policy())

    # then
    assert paths == frozenset(carried)


# --------------------------------------------------------------------------- properties

_LEAVES = st.none() | st.booleans() | st.integers() | st.text(max_size=6)
_KEYS = st.sampled_from(["a", "b", "c", "x", "y", "z"])
_VALUES = st.dictionaries(_KEYS, _LEAVES | st.dictionaries(_KEYS, _LEAVES, max_size=3), max_size=5)
_ALLOWED = frozenset({("a",), ("b",)})
_ALLOW_POLICY = _policy(synced=_ALLOWED)


def _outside(value: dict) -> dict:
    return {key: item for key, item in value.items() if (key,) not in _ALLOWED}


@settings(deadline=None)
@given(value=_VALUES)
def test_a_projection_only_holds_allowlisted_keys(value) -> None:
    # when
    projected = _loaded(project_text(ADAPTER, json.dumps(value), _ALLOW_POLICY))

    # then
    assert _outside(projected) == {}


@settings(deadline=None)
@given(value=_VALUES)
def test_an_allowlist_projection_is_idempotent(value) -> None:
    # when
    once = project_text(ADAPTER, json.dumps(value), _ALLOW_POLICY)
    twice = project_text(ADAPTER, once, _ALLOW_POLICY)

    # then
    assert twice == once


@settings(deadline=None)
@given(shared=_VALUES, local=_VALUES)
def test_restore_always_projects_back_to_the_repo_text(shared, local) -> None:
    # given — the repo holds a projection; this machine's file is any document
    repo_text = project_text(ADAPTER, json.dumps(shared), _ALLOW_POLICY)

    # when
    restored = restore_text(ADAPTER, repo_text, json.dumps(local), _ALLOW_POLICY)

    # then
    assert project_text(ADAPTER, restored, _ALLOW_POLICY) == repo_text


@settings(deadline=None)
@given(shared=_VALUES, local=_VALUES)
def test_a_restore_keeps_every_local_key_outside_the_allowlist(shared, local) -> None:
    # given
    repo_text = project_text(ADAPTER, json.dumps(shared), _ALLOW_POLICY)

    # when
    restored = _loaded(restore_text(ADAPTER, repo_text, json.dumps(local), _ALLOW_POLICY))

    # then
    assert _outside(restored) == _outside(local)


@settings(deadline=None)
@given(value=_VALUES, other=_VALUES)
def test_a_change_outside_the_allowlist_never_changes_the_projection(value, other) -> None:
    # given
    first = {**value, **_outside(value)}
    second = {**{k: v for k, v in value.items() if (k,) in _ALLOWED}, **_outside(other)}

    # when
    projected = [project_text(ADAPTER, json.dumps(doc), _ALLOW_POLICY) for doc in (first, second)]

    # then
    assert projected[0] == projected[1]
