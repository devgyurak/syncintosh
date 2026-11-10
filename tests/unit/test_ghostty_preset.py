# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the built-in `ghostty` preset: its shape, its place in the catalog, and the text merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.presets import BUILTIN_PRESETS, BuiltinCatalog
from syncintosh.presets.ghostty import CONFIG_FILES, GHOSTTY, THEMES

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ghostty" / "config"
GHOSTTY_PATHS = (*CONFIG_FILES, THEMES.replace("*", "fixture-dark"))


def _keybinds(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("keybind")]


def test_every_file_is_text_with_the_ghostty_check_and_no_strip_list() -> None:
    # when
    shapes = [(each.format, each.validator, each.noise, each.excluded) for each in GHOSTTY.files]

    # then — a repeated key cannot be stripped by key path, so the validator is the only gate
    assert shapes == [("text", "ghostty", frozenset(), frozenset())] * 5


def test_the_preset_names_the_verified_app_and_download() -> None:
    # when & then
    assert (GHOSTTY.bundle_id, GHOSTTY.download_url, GHOSTTY.cask) == (
        "com.mitchellh.ghostty",
        "https://ghostty.org/download",
        "ghostty",
    )


def test_the_tracked_paths_are_the_four_files_and_one_themes_glob() -> None:
    # when & then
    assert [each.path for each in GHOSTTY.files] == [*CONFIG_FILES, THEMES]


@pytest.mark.parametrize("path", GHOSTTY_PATHS)
def test_the_catalog_resolves_each_path_to_the_ghostty_policy(path) -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    policy = catalog.policy_for(path)

    # then
    assert (policy is not None and policy.validator, catalog.knows("ghostty", path)) == ("ghostty", True)


def test_no_other_preset_claims_a_ghostty_path() -> None:
    # given
    others = [preset for preset in BUILTIN_PRESETS if preset.name != "ghostty"]

    # when — a second claimant would shadow this preset's check, and the catalog refuses it at build time
    claimed = [
        (preset.name, path)
        for preset in others
        for path in GHOSTTY_PATHS
        if any(each.path == path or (each.glob is not None and each.glob.matches(path)) for each in preset.files)
    ]

    # then
    assert claimed == []


def test_a_path_outside_the_four_files_and_the_glob_is_not_ghostty() -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    foreign = [
        catalog.knows("ghostty", path)
        for path in (".config/ghostty/other", "Library/Application Support/com.mitchellh.ghostty/themes/x")
    ]

    # then — App Support has no themes directory Ghostty reads
    assert foreign == [False, False]


def test_a_merge_of_two_keybind_changes_keeps_every_line_and_its_order() -> None:
    # given — one side inserts a binding after the first, the other appends one at the end
    base = FIXTURE.read_text(encoding="utf-8")
    first, last = _keybinds(base)[0], _keybinds(base)[-1]
    local = base.replace(first, f"{first}\nkeybind = cmd+n=new_window")
    remote = base.replace(last, f"{last}\nkeybind = cmd+j=jump_to_prompt:1")

    # when
    merged = TextMergeDriver().merge(base, local, remote)

    # then
    original = _keybinds(base)
    assert (merged.conflicted, _keybinds(merged.text)) == (
        False,
        [original[0], "keybind = cmd+n=new_window", *original[1:], "keybind = cmd+j=jump_to_prompt:1"],
    )


def test_the_fixture_carries_the_documented_keys() -> None:
    # given
    keys = {line.partition("=")[0].strip() for line in FIXTURE.read_text(encoding="utf-8").splitlines() if "=" in line}

    # when
    expected = {
        "keybind", "font-family", "font-family-bold", "font-family-italic", "font-size", "font-thicken", "theme",
        "background-opacity", "background-blur", "cursor-style", "cursor-style-blink", "copy-on-select",
        "click-repeat-interval", "mouse-hide-while-typing", "scrollback-limit", "shell-integration",
        "shell-integration-features", "macos-option-as-alt", "macos-titlebar-style", "macos-window-shadow",
        "unfocused-split-opacity", "window-colorspace", "window-padding-x", "window-padding-y",
        "window-padding-balance", "window-save-state",
    }  # fmt: skip

    # then
    assert (sorted(expected - keys), len(_keybinds(FIXTURE.read_text(encoding="utf-8")))) == ([], 14)
