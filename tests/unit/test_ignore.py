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
    assert (spec.keys, spec.unmatched_cancellations) == (frozenset(), ())


def test_a_cancellation_across_the_two_names_still_cancels() -> None:
    # when
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["a"], LEGACY_IGNORED_SETTINGS_KEY: ["-a"]})

    # then
    assert spec.keys == frozenset()


def test_a_cancellation_with_nothing_to_cancel_is_reported_not_applied() -> None:
    # when — `-b` names a key the user never added: upstream it would subtract from a built-in default
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["a", "-b", "-c"]})

    # then
    assert (spec.keys, spec.unmatched_cancellations) == ({"a"}, ("b", "c"))


@pytest.mark.parametrize("key", [IGNORED_SETTINGS_KEY, "settingsSync.ignoredExtensions"])
def test_the_keys_that_cannot_be_ignored_are_never_carried(key) -> None:
    # when
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: [key, "editor.fontSize"]})

    # then — the list itself travels
    assert spec.keys == {"editor.fontSize"}


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
    ("policy", "paths"),
    [
        (_policy(), {("a",), ("b",)}),
        (_policy(noise={("a",)}), {("b",)}),
        (_policy(excluded={("a",)}), {("b",)}),
        (_policy(noise={("a",)}, excluded={("b",)}), set()),
        # A noise path *below* an ignored key does not stop the key being carried; the second strip drops the noise.
        (_policy(noise={("a", "x")}), {("a",), ("b",)}),
    ],
)
def test_a_key_the_preset_strips_is_not_also_carried(policy, paths) -> None:
    # given
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["a", "b"]})

    # when & then
    assert spec.paths_for(policy) == paths


def test_the_empty_spec_carries_nothing() -> None:
    # when & then
    assert NO_IGNORES.paths_for(_policy()) == frozenset()
