# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the user's ignore list: which top-level settings a machine carries instead of syncing."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import IgnoreListMalformed
from syncintosh.core.ignore import IGNORED_SETTINGS_KEY, LEGACY_IGNORED_SETTINGS_KEY, NO_IGNORES, IgnoreSpec
from syncintosh.core.presets import PresetFile


def _policy(*, noise=(), excluded=()) -> PresetFile:
    return PresetFile.create(path="settings.json", format="jsonc", noise=noise, excluded=excluded)


@pytest.mark.parametrize(
    ("settings", "keys"),
    [
        ({}, set()),
        ({IGNORED_SETTINGS_KEY: []}, set()),
        ({IGNORED_SETTINGS_KEY: ["editor.fontSize", "[python]"]}, {"editor.fontSize", "[python]"}),
        ({LEGACY_IGNORED_SETTINGS_KEY: ["editor.fontSize"]}, {"editor.fontSize"}),
        (
            {IGNORED_SETTINGS_KEY: ["a"], LEGACY_IGNORED_SETTINGS_KEY: ["b"]},
            {"a", "b"},
        ),
    ],
)
def test_the_ignored_keys_are_read_from_both_names(settings, keys) -> None:
    # when
    spec = IgnoreSpec.create(settings)

    # then
    assert spec.keys == keys


@pytest.mark.parametrize("entries", [["-a", "a"], ["a", "-a"]])
def test_a_cancellation_removes_the_users_own_entry_whatever_the_order(entries) -> None:
    # when
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: entries})

    # then
    assert spec.paths_for(_policy()) == frozenset()


def test_a_cancellation_across_the_two_names_still_cancels() -> None:
    # when
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["a"], LEGACY_IGNORED_SETTINGS_KEY: ["-a"]})

    # then
    assert spec.paths_for(_policy()) == frozenset()


def test_a_cancellation_is_kept_by_name() -> None:
    # when
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["a", "-b", "-c"]})

    # then
    assert (spec.keys, spec.cancelled) == ({"a"}, {"b", "c"})


@pytest.mark.parametrize("key", [IGNORED_SETTINGS_KEY, "settingsSync.ignoredExtensions"])
def test_the_keys_that_cannot_be_ignored_are_never_carried(key) -> None:
    # when
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: [key, "editor.fontSize"]})

    # then — the list itself travels
    assert spec.paths_for(_policy()) == {("editor.fontSize",)}


@pytest.mark.parametrize(
    "value",
    ["editor.fontSize", None, {"a": 1}, 3, ["ok", 1], ["ok", None], [["nested"]]],
)
@pytest.mark.parametrize("name", [IGNORED_SETTINGS_KEY, LEGACY_IGNORED_SETTINGS_KEY])
def test_a_list_that_is_not_an_array_of_strings_fails_closed(name, value) -> None:
    # when & then
    with pytest.raises(IgnoreListMalformed):
        IgnoreSpec.create({name: value})


def test_the_malformed_message_never_quotes_the_list() -> None:
    # when
    with pytest.raises(IgnoreListMalformed) as raised:
        IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["SECRET-VALUE", 1]})

    # then
    assert "SECRET-VALUE" not in str(raised.value)


@pytest.mark.parametrize(
    ("entries", "policy", "paths"),
    [
        (["a", "b"], _policy(), {("a",), ("b",)}),
        # The preset's noise is the default ignore set: carried without being listed.
        ([], _policy(noise={("n",)}), {("n",)}),
        (["a"], _policy(noise={("n",)}), {("a",), ("n",)}),
        (["a", "n"], _policy(noise={("n",)}), {("a",), ("n",)}),
        # `-x` beats the preset's noise and the user's own entry alike.
        (["-n"], _policy(noise={("n",)}), set()),
        (["a", "-a", "-n"], _policy(noise={("n",)}), set()),
        # Excluded always wins: it is stripped, never carried, and `-x` does not release it.
        (["a", "b"], _policy(excluded={("a",)}), {("b",)}),
        (["-a"], _policy(excluded={("a",)}), set()),
        (["a"], _policy(noise={("n",)}, excluded={("a",)}), {("n",)}),
        # An excluded path *below* an ignored key does not stop the key being carried; the second strip drops it.
        (["a"], _policy(excluded={("a", "x")}), {("a",)}),
        # A key outside the list's own reach is never carried.
        (["settingsSync.ignoredSettings"], _policy(), set()),
    ],
)
def test_the_carried_keys_follow_the_precedence(entries, policy, paths) -> None:
    # given
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: entries})

    # when & then
    assert spec.paths_for(policy) == paths


def test_the_empty_spec_carries_the_noise() -> None:
    # when & then
    assert NO_IGNORES.paths_for(_policy(noise={("n",)})) == {("n",)}
    assert NO_IGNORES.paths_for(_policy()) == frozenset()


@pytest.mark.parametrize(
    ("entries", "count"),
    [
        (["-x"], 1),
        (["-x", "-x"], 1),
        (["-x", "-http.proxyAuthorization"], 2),
        (["-y", "x"], 0),
        ([], 0),
    ],
)
def test_only_a_cancellation_aimed_at_an_excluded_key_is_blocked(entries, count) -> None:
    # given
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: entries})
    policy = _policy(excluded={("x",), ("http.proxyAuthorization",)})

    # when & then
    assert spec.blocked_cancellations(policy) == count
