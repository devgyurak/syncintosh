# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the built-in `iterm2` preset: its shape, its place in the catalog, and the per-profile merge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from syncintosh.core.keyed_array import ArrayRule, KeyedMerge, merge_keyed_array
from syncintosh.core.tree import Array, Tree, tree_from_value
from syncintosh.presets import BUILTIN_PRESETS, BuiltinCatalog
from syncintosh.presets.iterm2 import DYNAMIC_PROFILES, ITERM2

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "iterm2" / "dynamic-profiles.json"
PROFILE_PATH = DYNAMIC_PROFILES.replace("*", "work")
RULE = ArrayRule.create(identity=("Guid",), required=("Guid",), group=("Guid",))


def _profiles(*changes: dict[str, object]) -> Array:
    """The fixture's `Profiles` as the array leaf the merge reads, with `changes` applied by Guid or appended."""
    profiles = json.loads(FIXTURE.read_text(encoding="utf-8"))["Profiles"]
    by_guid = {each["Guid"]: each for each in profiles}
    by_guid.update({each["Guid"]: {**by_guid.get(each["Guid"], {}), **each} for each in changes})
    leaf = tree_from_value(list(by_guid.values())).value(())
    assert isinstance(leaf, Array)
    return leaf


def _plain(leaf: Array) -> Any:
    return Tree.create({(): leaf}).to_value()


def _settled(base: Array, local: Array, remote: Array) -> KeyedMerge:
    outcome = merge_keyed_array(base, local, remote, RULE)
    assert isinstance(outcome, KeyedMerge)
    return outcome


def test_the_preset_names_the_verified_app_and_download() -> None:
    # when & then
    assert (ITERM2.bundle_id, ITERM2.download_url, ITERM2.cask) == (
        "com.googlecode.iterm2",
        "https://iterm2.com/downloads.html",
        "iterm2",
    )


def test_the_only_tracked_path_is_the_json_glob_with_no_strip_list() -> None:
    # when
    shapes = [(each.path, each.format, each.noise, each.excluded) for each in ITERM2.files]

    # then — and a live-reloaded file never holds apply back while iTerm2 runs
    assert (shapes, ITERM2.defer_while_running) == ([(DYNAMIC_PROFILES, "jsonc", frozenset(), frozenset())], False)


def test_profiles_are_identified_by_guid() -> None:
    # when
    rules = ITERM2.files[0].array_rules

    # then
    assert rules == {("Profiles",): RULE}


def test_the_catalog_resolves_a_profile_file_to_the_iterm2_policy() -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    policy = catalog.policy_for(PROFILE_PATH)

    # then
    assert (policy is not None and policy.path, catalog.knows("iterm2", PROFILE_PATH)) == (DYNAMIC_PROFILES, True)


@pytest.mark.parametrize(
    "path",
    [
        "Library/Application Support/iTerm2/DynamicProfiles/work.plist",
        "Library/Application Support/iTerm2/DynamicProfiles/nested/work.json",
        "Library/Application Support/iTerm2/Scripts/work.json",
    ],
)
def test_a_path_outside_the_json_glob_is_not_iterm2(path) -> None:
    # when
    owned = BuiltinCatalog.default().knows("iterm2", path)

    # then
    assert owned is False


def test_no_other_preset_claims_the_profile_directory() -> None:
    # given
    others = [preset for preset in BUILTIN_PRESETS if preset.name != "iterm2"]

    # when
    claimed = [preset.name for preset in others for each in preset.files if each.matches(PROFILE_PATH)]

    # then
    assert claimed == []


def test_a_profile_added_on_each_machine_is_kept() -> None:
    # given
    base = _profiles()
    local = _profiles({"Guid": "11111111-AAAA-4AAA-8AAA-000000000003", "Name": "Local Only"})
    remote = _profiles({"Guid": "11111111-AAAA-4AAA-8AAA-000000000004", "Name": "Remote Only"})

    # when
    result = _settled(base, local, remote)

    # then
    assert (result.conflicts, sorted(each["Name"] for each in _plain(result.merged))) == (
        (),
        ["Fixture Logs", "Fixture Work", "Local Only", "Remote Only"],
    )


def test_a_one_sided_edit_of_a_profile_wins() -> None:
    # given
    guid = "11111111-AAAA-4AAA-8AAA-000000000001"
    base = _profiles()
    remote = _profiles({"Guid": guid, "Badge Text": "renamed"})

    # when
    result = _settled(base, base, remote)

    # then
    assert (result.conflicts, [each["Badge Text"] for each in _plain(result.merged)]) == ((), ["renamed", "logs"])


def test_the_same_profile_changed_differently_on_both_sides_is_a_conflict_on_that_profile_only() -> None:
    # given
    guid = "11111111-AAAA-4AAA-8AAA-000000000001"
    base = _profiles()
    local = _profiles({"Guid": guid, "Badge Text": "local"})
    remote = _profiles(
        {"Guid": guid, "Badge Text": "remote"}, {"Guid": "11111111-AAAA-4AAA-8AAA-000000000002", "Badge Text": "kept"}
    )

    # when
    result = _settled(base, local, remote)

    # then — the other profile took the remote edit; the conflicting one keeps the local candidate
    assert ([each.element for each in result.conflicts], [each["Badge Text"] for each in _plain(result.merged)]) == (
        [(("Guid", guid),)],
        ["local", "kept"],
    )
