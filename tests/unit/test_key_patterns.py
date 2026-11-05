# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for key patterns: `ANY_KEY` in a preset's noise/excluded keys (issue #53, part A).

A pattern segment stands for exactly one object key of any name, so a credential that lives under a name the
user chose (`context_servers.<name>.env`) can be excluded. The matching is core's, the sealed text is the
JSONC adapter's, and the properties run over generated documents (``AGENTS.md`` §5.3).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ConfigError
from syncintosh.core.ignore import IgnoreSpec
from syncintosh.core.policy import policy_digest
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.projection import NO_CARRY, Carry, graft, project_text, restore_text, strip
from syncintosh.core.tree import ANY_KEY, AnyKey, tree_from_value

ADAPTER = JsoncAdapter()

ENV = ("servers", ANY_KEY, "env")
BINARY = ("servers", ANY_KEY, "path")
NESTED_ENV = ("profiles", ANY_KEY, "settings", "terminal", "env")


def _policy(*, noise=(), excluded=(), ignore_list: bool = False) -> PresetFile:
    return PresetFile.create(
        path="settings.json", format="jsonc", noise=noise, excluded=excluded, ignore_list=ignore_list
    )


def _project(text: str, policy: PresetFile, carried: Carry = NO_CARRY) -> str:
    return project_text(ADAPTER, text, policy, carried)


SERVERS = (
    "{\n"
    '  "servers": {\n'
    '    "alpha": { "command": "a", "env": { "TOKEN": "FAKE-ALPHA" }, "path": "/opt/a" },\n'
    '    "beta": { "command": "b", "env": { "TOKEN": "FAKE-BETA" } }\n'
    "  },\n"
    '  "theme": "dark"\n'
    "}\n"
)


# --------------------------------------------------------------------------- strip / graft


def test_a_pattern_strips_the_key_under_every_name_and_keeps_the_siblings() -> None:
    # given
    tree = tree_from_value(
        {"servers": {"a": {"env": {"K": "1"}, "command": "x"}, "b": {"env": {"K": "2"}, "command": "y"}}}
    )

    # when
    result = strip(tree, frozenset({ENV}))

    # then
    assert list(result) == [("servers", "a", "command"), ("servers", "b", "command")]


def test_a_wildcard_matches_exactly_one_key_not_none_and_not_two() -> None:
    # given — `env` one level too high, and one level too deep
    tree = tree_from_value({"servers": {"env": {"K": "1"}}, "x": {"servers": {"a": {"env": 1}}}})

    # when
    result = strip(tree, frozenset({ENV}))

    # then
    assert list(result) == [("servers", "env", "K"), ("x", "servers", "a", "env")]


def test_a_wildcard_in_the_middle_of_a_prefix_strips_the_deeper_key() -> None:
    # given
    tree = tree_from_value(
        {"profiles": {"work": {"settings": {"terminal": {"env": {"K": "v"}, "font": 1}}}, "home": {"base": 1}}}
    )

    # when
    result = strip(tree, frozenset({NESTED_ENV}))

    # then
    assert list(result) == [("profiles", "work", "settings", "terminal", "font"), ("profiles", "home", "base")]


def test_graft_re_attaches_a_pattern_match_in_the_live_order() -> None:
    # given — the repo lacks the env blocks; the live file has them between the other keys
    repo = tree_from_value({"servers": {"a": {"command": "x"}, "b": {"command": "y"}}})
    live = tree_from_value({"servers": {"a": {"env": {"K": "1"}, "command": "x"}, "b": {"command": "y"}}})

    # when
    result = graft(repo, live, frozenset({ENV}))

    # then
    assert list(result) == [("servers", "a", "env", "K"), ("servers", "a", "command"), ("servers", "b", "command")]


def test_a_real_key_named_star_is_carried_by_exact_path_and_is_no_wildcard() -> None:
    # given — the user ignores a key literally called `*`; the policy strips `servers.<any>.env`
    live = {"*": 1, "other": 2, "servers": {"a": {"env": {"K": "1"}, "command": "x"}}}
    policy = _policy(excluded={ENV})
    carried = Carry(frozenset({("*",)}), json.dumps({"*": 100}))

    # when
    sealed = json.loads(_project(json.dumps(live), policy, carried))

    # then — `*` takes the source's value, `other` is untouched, and the pattern still cut the env
    assert sealed == {"*": 100, "other": 2, "servers": {"a": {"command": "x"}}}


def test_restore_keeps_a_carried_star_key_exact_and_patterns_apart() -> None:
    # given
    policy = _policy(excluded={ENV})
    repo_text = '{"*": 100, "servers": {"a": {"command": "x"}}}\n'
    live_text = '{"*": 7, "servers": {"a": {"env": {"K": "1"}, "command": "x"}}}\n'

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, policy, Carry(frozenset({("*",)}), repo_text))

    # then
    assert json.loads(restored) == {"*": 7, "servers": {"a": {"env": {"K": "1"}, "command": "x"}}}


def test_an_ignore_list_entry_that_is_a_star_is_an_exact_key() -> None:
    # given
    spec = IgnoreSpec.create({"settingsSync.ignoredSettings": ["*", "env"]})

    # when
    paths = spec.paths_for(_policy(excluded={ENV}))

    # then — carried as the keys they spell, neither expanded nor dropped by a pattern
    assert paths == frozenset({("*",), ("env",)})


# --------------------------------------------------------------------------- sealed text


def test_project_text_seals_no_credential_under_any_server_name() -> None:
    # given
    policy = _policy(excluded={ENV}, noise={BINARY})

    # when
    sealed = _project(SERVERS, policy)

    # then
    assert [secret for secret in ("FAKE-ALPHA", "FAKE-BETA", "/opt/a") if secret in sealed] == []
    assert json.loads(sealed) == {"servers": {"alpha": {"command": "a"}, "beta": {"command": "b"}}, "theme": "dark"}


def test_a_comment_inside_an_excluded_subtree_does_not_stop_the_projection_and_is_not_sealed() -> None:
    # given — #83: the member the projection strips is not a refusal target; its comment may hold a credential
    text = (
        "{\n"
        '  "servers": {\n'
        '    "a": {\n'
        '      "command": "x",\n'
        '      "env": { // FAKE-COMMENT-TOKEN\n'
        '        "K": "v"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )

    # when
    sealed = _project(text, _policy(excluded={ENV}))

    # then
    assert "FAKE-COMMENT-TOKEN" not in sealed
    assert json.loads(sealed) == {"servers": {"a": {"command": "x"}}}


def test_restore_re_attaches_the_pattern_matches_and_projects_back_to_the_repo() -> None:
    # given
    policy = _policy(excluded={ENV}, noise={BINARY})
    repo_text = _project(SERVERS, policy)
    live_text = SERVERS.replace("FAKE-ALPHA", "FAKE-LOCAL").replace("/opt/a", "/usr/local/a")

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, policy)

    # then
    assert "FAKE-LOCAL" in restored
    assert "/usr/local/a" in restored
    assert _project(restored, policy) == repo_text


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("noise", "excluded", "ignore_list"),
    [
        ({(ANY_KEY,)}, (), False),
        ((), {(ANY_KEY, ANY_KEY)}, False),
        ({ENV}, {ENV}, False),
        ((), {ENV}, True),
        ({BINARY}, (), True),
        ({()}, (), False),
    ],
)
def test_create_refuses_a_pattern_that_cannot_be_trusted(noise, excluded, ignore_list) -> None:
    # when & then
    with pytest.raises(ConfigError):
        _policy(noise=noise, excluded=excluded, ignore_list=ignore_list)


def test_the_duplicate_error_names_overlapping_patterns_that_mix_names_and_wildcards() -> None:
    # given — a name and a wildcard meet at one position, which `AnyKey` cannot be ordered across
    shared = {("servers", "a", "env"), ("servers", ANY_KEY, "env")}

    # when & then
    with pytest.raises(ConfigError, match="both noise and excluded"):
        _policy(noise=shared, excluded=shared)


def test_a_second_wildcard_instance_is_still_a_wildcard() -> None:
    # given — the sentinel compares by value, so a separately built one must behave as `ANY_KEY` does
    tree = tree_from_value({"servers": {"a": {"env": 1, "command": "x"}}})

    # when
    result = strip(tree, frozenset({("servers", AnyKey(), "env")}))

    # then
    assert list(result) == [("servers", "a", "command")]


def test_create_accepts_a_pattern_with_a_literal_key_anywhere() -> None:
    # when
    policy = _policy(noise={(ANY_KEY, "path")}, excluded={ENV, NESTED_ENV})

    # then
    assert policy.stripped == frozenset({(ANY_KEY, "path"), ENV, NESTED_ENV})


# --------------------------------------------------------------------------- policy digest


def _preset(*, excluded) -> Preset:
    return Preset.create(name="p", files=(_policy(excluded=excluded),))


def test_a_wildcard_and_a_key_called_star_have_different_digests() -> None:
    # when
    wildcard = policy_digest(_preset(excluded={("servers", ANY_KEY, "env")}))
    literal = policy_digest(_preset(excluded={("servers", "*", "env")}))

    # then
    assert wildcard != literal


def test_the_digest_does_not_depend_on_the_order_patterns_were_listed_in() -> None:
    # when
    first = policy_digest(_preset(excluded=[ENV, ("servers", "x"), NESTED_ENV]))
    second = policy_digest(_preset(excluded=[NESTED_ENV, ("servers", "x"), ENV]))

    # then
    assert first == second


# --------------------------------------------------------------------------- properties

_LEAVES = st.none() | st.booleans() | st.integers() | st.text(max_size=5)
_NAMES = st.sampled_from(["a", "b", "*", "env", "path"])
_POLICY = _policy(excluded={ENV}, noise={BINARY})
_INDENT = st.sampled_from([None, 2])


def _server(command, path, env) -> dict:
    return {"command": command, "path": path, "env": {"K": env}}


def _document(names, command, path, env, indent) -> str:
    servers = {name: _server(command, path, env) for name in names}
    return json.dumps({"servers": servers, "theme": "dark"}, indent=indent, ensure_ascii=False)


_NAME_SETS = st.lists(_NAMES, min_size=1, max_size=3, unique=True)


@given(names=_NAME_SETS, command=_LEAVES, path=_LEAVES, env=_LEAVES, indent=_INDENT)
def test_a_pattern_projection_is_idempotent(names, command, path, env, indent) -> None:
    # when
    once = _project(_document(names, command, path, env, indent), _POLICY)
    twice = _project(once, _POLICY)

    # then
    assert twice == once


@given(names=_NAME_SETS, command=_LEAVES, before=_LEAVES, after=_LEAVES, indent=_INDENT)
def test_a_change_to_a_matched_key_does_not_change_the_projection(names, command, before, after, indent) -> None:
    # when — the documents differ only in the matched keys' values
    first = _project(_document(names, command, before, before, indent), _POLICY)
    second = _project(_document(names, command, after, after, indent), _POLICY)

    # then
    assert first == second


@given(names=_NAME_SETS, command=_LEAVES, local=_LEAVES, indent=_INDENT)
def test_a_pattern_restore_projects_back_to_the_repo_text(names, command, local, indent) -> None:
    # given
    repo_text = _project(_document(names, command, "sealed", "sealed", indent), _POLICY)
    live_text = _document(names, command, local, local, indent)

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, _POLICY)

    # then
    assert _project(restored, _POLICY) == repo_text
