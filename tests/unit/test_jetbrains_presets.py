# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the catalog of JetBrains IDE presets: metadata, the shared file set, and uniqueness (issue #54)."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from syncintosh.core.config_dirs import ConfigDirSpec
from syncintosh.core.paths import collision_key
from syncintosh.core.presets import Preset
from syncintosh.presets import BUILTIN_PRESETS, BuiltinCatalog, datagrip, jetbrains
from syncintosh.presets.jetbrains_ides import JETBRAINS_IDES

CATALOG = BuiltinCatalog.default()
IDE_PRESETS = (datagrip.DATAGRIP, *JETBRAINS_IDES)

# name -> (folder prefix, bundle id, cask, download page): each read from the cask JSON and the vendor's help page.
EXPECTED = {
    "datagrip": ("DataGrip", "com.jetbrains.datagrip", "datagrip", "https://www.jetbrains.com/datagrip/download/"),
    "clion": ("CLion", "com.jetbrains.CLion", "clion", "https://www.jetbrains.com/clion/download/"),
    "goland": ("GoLand", "com.jetbrains.goland", "goland", "https://www.jetbrains.com/go/download/"),
    "intellij-idea": (
        "IntelliJIdea",
        "com.jetbrains.intellij",
        "intellij-idea",
        "https://www.jetbrains.com/idea/download/",
    ),
    "phpstorm": ("PhpStorm", "com.jetbrains.PhpStorm", "phpstorm", "https://www.jetbrains.com/phpstorm/download/"),
    "pycharm": ("PyCharm", "com.jetbrains.pycharm", "pycharm", "https://www.jetbrains.com/pycharm/download/"),
    "rubymine": ("RubyMine", "com.jetbrains.rubymine", "rubymine", "https://www.jetbrains.com/ruby/download/"),
    "rustrover": ("RustRover", "com.jetbrains.rustrover", "rustrover", "https://www.jetbrains.com/rust/download/"),
    "webstorm": ("WebStorm", "com.jetbrains.WebStorm", "webstorm", "https://www.jetbrains.com/webstorm/download/"),
}
EXACT_COUNT = len(jetbrains.COMMON_OPTIONS)
GLOB_COUNT = len(jetbrains.COMMON_GLOBS)


def _prefix(preset: Preset) -> str:
    assert preset.config_dir is not None
    return preset.config_dir.prefix


def test_every_jetbrains_ide_preset_is_built_in_and_none_is_missing() -> None:
    # when
    names = sorted(preset.name for preset in IDE_PRESETS)

    # then
    assert names == sorted(EXPECTED)
    assert [preset for preset in IDE_PRESETS if preset not in BUILTIN_PRESETS] == []


@pytest.mark.parametrize("preset", IDE_PRESETS, ids=lambda preset: preset.name)
def test_the_metadata_is_the_verified_one(preset) -> None:
    # given
    prefix, bundle_id, cask, url = EXPECTED[preset.name]

    # when
    actual = (preset.bundle_id, preset.cask, preset.download_url)

    # then
    assert actual == (bundle_id, cask, url)
    assert preset.config_dir == ConfigDirSpec.create(
        home=jetbrains.HOME,
        prefix=prefix,
        vendor=jetbrains.VENDOR,
        bundle_id=bundle_id,
        sync_conflict=jetbrains.SYNC_CONFLICT,
    )
    assert preset.defer_while_running is True


@pytest.mark.parametrize("preset", IDE_PRESETS, ids=lambda preset: preset.name)
def test_the_download_page_is_an_https_page_on_the_vendors_domain(preset) -> None:
    # when
    parsed = urlparse(preset.download_url or "")

    # then
    assert (parsed.scheme, parsed.hostname) == ("https", "www.jetbrains.com")
    assert parsed.path.endswith("/download/")


@pytest.mark.parametrize("preset", JETBRAINS_IDES, ids=lambda preset: preset.name)
def test_a_product_without_a_confirmed_extra_file_tracks_exactly_the_common_set(preset) -> None:
    # given
    spec = preset.config_dir
    assert spec is not None

    # when
    paths = [file.path for file in preset.files]

    # then
    assert paths == [
        spec.logical_path(relative) for relative in (*jetbrains.COMMON_OPTIONS, *(g for g, _ in jetbrains.COMMON_GLOBS))
    ]
    assert len(paths) == EXACT_COUNT + GLOB_COUNT


def test_no_two_presets_claim_one_path_and_every_logical_path_is_unique() -> None:
    # when
    folded = [collision_key(file.path) for preset in IDE_PRESETS for file in preset.files]
    duplicates = sorted({path for path in folded if folded.count(path) > 1})

    # then
    assert duplicates == []


def test_bundle_ids_casks_names_and_prefixes_are_unique() -> None:
    # when
    columns = [
        [collision_key(value) for value in values]
        for values in zip(
            *[(preset.name, preset.bundle_id or "", preset.cask or "", _prefix(preset)) for preset in IDE_PRESETS],
            strict=True,
        )
    ]

    # then
    assert [column for column in columns if len(set(column)) != len(column)] == []


@pytest.mark.parametrize("preset", IDE_PRESETS, ids=lambda preset: preset.name)
def test_a_product_never_names_a_file_that_must_not_travel(preset) -> None:
    # given
    never = ("remote-servers", "sshConfigs", "webServers", "dataSources", "security", "github", "gitlab", "docker")

    # when
    leaked = [file.path for file in preset.files if any(f"/{name}" in file.path for name in never)]

    # then
    assert leaked == []


def test_the_default_catalog_builds_with_every_product() -> None:
    # when
    owners = {
        name: CATALOG.preset_for(f"{jetbrains.HOME}/@{prefix}/options/editor-font.xml")
        for name, (prefix, *_) in EXPECTED.items()
    }

    # then
    assert {name: preset.name if preset else None for name, preset in owners.items()} == {
        name: name for name in EXPECTED
    }
