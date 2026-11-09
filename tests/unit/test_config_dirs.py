# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for versioned config directories: the layout codec, the preset rules, and the catalog's overlap check.

The layout is a TEST-ONLY one (`Widget` under a made-up vendor folder); the real JetBrains preset is a later change.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.config_dirs import ConfigDirMap, ConfigDirSpec
from syncintosh.core.errors import ConfigError
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.profiles import ProfileFile, ProfileSpec
from syncintosh.presets import BuiltinCatalog

HOME = "Library/Application Support/Acme"
BUNDLE = "com.acme.widget"


def _spec(**overrides: str) -> ConfigDirSpec:
    fields = {"home": HOME, "prefix": "Widget", "vendor": "Acme", "bundle_id": BUNDLE} | overrides
    return ConfigDirSpec.create(**fields)


def _preset(spec: ConfigDirSpec | None = None, *, name: str = "widget", paths: tuple[str, ...] | None = None, **kw):
    layout = spec or _spec()
    chosen = paths if paths is not None else (layout.logical_path("options/editor.xml"),)
    return Preset.create(
        name=name,
        files=tuple(PresetFile.create(path=path, format="text") for path in chosen),
        bundle_id=layout.bundle_id,
        config_dir=layout,
        **kw,
    )


# --------------------------------------------------------------------------- the layout


@pytest.mark.parametrize(
    "overrides",
    [
        {"prefix": ""},
        {"prefix": "9Widget"},
        {"prefix": "Wid/get"},
        {"prefix": "Wid get"},
        {"home": ""},
        {"home": "/abs"},
        {"home": "a/../b"},
        {"vendor": ""},
        {"bundle_id": ""},
        {"bundle_id": "bad id"},
        {"bundle_id": "a'b"},
    ],
)
def test_a_layout_with_a_bad_field_is_refused(overrides: dict[str, str]) -> None:
    # when & then
    with pytest.raises(ConfigError):
        _spec(**overrides)


def test_the_logical_root_and_path_carry_the_marker() -> None:
    # given
    spec = _spec()

    # when & then
    assert spec.root == f"{HOME}/@Widget"
    assert spec.logical_path("options/editor.xml") == f"{HOME}/@Widget/options/editor.xml"


@pytest.mark.parametrize(
    ("path", "relative"),
    [
        (f"{HOME}/@Widget/options/editor.xml", "options/editor.xml"),
        (f"{HOME}/@Widget/a", "a"),
        (f"{HOME}/@Widget", None),
        (f"{HOME}/@Widget/", None),
        (f"{HOME}/@widget/options/editor.xml", None),
        (f"{HOME}/Widget2026.2/options/editor.xml", None),
        (f"{HOME}/@Widget/options/../x", None),
        (f"{HOME}/@Widget/options/*.xml", None),
        (f"{HOME}/@WidgetX/options/editor.xml", None),
        ("elsewhere/@Widget/options/editor.xml", None),
    ],
)
def test_parse_accepts_only_a_well_formed_logical_file(path: str, relative: str | None) -> None:
    # when
    parsed = _spec().parse(path)

    # then
    assert (None if parsed is None else parsed.relative) == relative


@pytest.mark.parametrize(
    ("directory", "relative"),
    [
        (f"{HOME}/@Widget", ""),
        (f"{HOME}/@Widget/options", "options"),
        (f"{HOME}/@Widget/a/b", "a/b"),
        (f"{HOME}/@Widget/a/*", None),
        (f"{HOME}/@Widget/../x", None),
        (f"{HOME}/Other", None),
    ],
)
def test_a_logical_directory_is_split_below_the_root(directory: str, relative: str | None) -> None:
    # when & then
    assert _spec().relative_directory(directory) == relative


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("Widget2026.2", True),
        ("Widget2026.10", True),
        ("Widget2026.", False),
        ("Widget26.2", False),
        ("Widget2026.2.1", False),
        ("Widget2026." + "9" * 4300, False),
        ("Widget2026.2\n", False),
        ("Widget2026.2/", False),
        ("../Widget2026.2", False),
        ("Widget2026.2/../x", False),
        ("widget2026.2", False),
        ("Widget٢٠٢٦.٢", False),
        ("WidgetCE2026.2", False),
        ("", False),
    ],
)
def test_only_a_plain_release_folder_name_is_valid(name: str, valid: bool) -> None:
    # when & then
    assert _spec().is_release_name(name) is valid


@given(st.text(max_size=30))
def test_any_valid_release_name_is_one_path_element_and_joins_below_home(text: str) -> None:
    # given
    spec = _spec()

    # when
    valid = spec.is_release_name(text)

    # then — a valid name never carries a separator or a dot-dot, so the real directory stays directly under home
    assert (not valid) or ("/" not in text and ".." not in text and spec.real_directory(text) == f"{HOME}/{text}")


@given(st.integers(2000, 2999), st.integers(0, 99), st.integers(2000, 2999), st.integers(0, 99))
def test_release_order_is_numeric_not_textual(y1: int, n1: int, y2: int, n2: int) -> None:
    # given
    spec = _spec()
    first, second = f"Widget{y1}.{n1}", f"Widget{y2}.{n2}"

    # when & then
    assert (spec.release_key(first) < spec.release_key(second)) == ((y1, n1) < (y2, n2))


def test_release_order_puts_ten_after_nine() -> None:
    # given
    spec = _spec()

    # when & then
    assert spec.release_key("Widget2026.10") > spec.release_key("Widget2026.9")


@pytest.mark.parametrize("call", ["release_key", "real_directory"])
def test_an_invalid_name_never_becomes_a_key_or_a_directory(call: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        getattr(_spec(), call)("Widget/../x")


@pytest.mark.parametrize(
    ("path", "owned"),
    [
        (f"{HOME}/Widget2026.2/options/a.xml", True),
        (f"{HOME.upper()}/WIDGET2026.2/x", True),
        (f"{HOME}/@Widget/options/a.xml", False),
        (f"{HOME}/Widget/options/a.xml", False),
        (f"{HOME}/Other2026.2/x", False),
        (HOME, False),
    ],
)
def test_a_real_release_path_is_recognised_for_the_overlap_check(path: str, owned: bool) -> None:
    # when & then
    assert _spec().owns_real(path) is owned


# --------------------------------------------------------------------------- the map


def test_a_resolved_map_names_its_folder_and_defaults_settings_sync_off() -> None:
    # when
    found = ConfigDirMap.resolved("Widget2026.2")

    # then
    assert (found.directory, found.settings_sync_enabled, found.reason) == ("Widget2026.2", False, "")


def test_an_unresolved_map_has_no_folder_and_says_why() -> None:
    # when
    found = ConfigDirMap.unresolved("not installed", ("note",))

    # then
    assert (found.directory, found.reason, found.problems) == (None, "not installed", ("note",))


@pytest.mark.parametrize("name", ["", None])
def test_a_resolved_map_without_a_name_is_refused(name) -> None:
    # when & then
    with pytest.raises(ConfigError):
        ConfigDirMap.resolved(name)


# --------------------------------------------------------------------------- the preset rules


def test_a_preset_may_own_files_in_a_versioned_config_directory() -> None:
    # when
    preset = _preset(defer_while_running=True)

    # then
    assert (preset.config_dir, preset.defer_while_running) == (_spec(), True)


def test_a_preset_cannot_combine_a_config_directory_with_profiles() -> None:
    # given
    profiles = ProfileSpec.create(
        default_root="a/User",
        home="a/User/profiles",
        index="a/storage.json",
        files=(ProfileFile.create(path="settings.json", resource="settings"),),
    )

    # when & then
    with pytest.raises(ConfigError):
        _preset(profiles=profiles, paths=(_spec().logical_path("x"), "a/User/settings.json"))


def test_a_config_directory_preset_cannot_hold_an_ignore_list_file() -> None:
    # given — the list would be read from the logical path text, so it would silently be empty
    path = _spec().logical_path("options/ignored.json")
    file = PresetFile.create(path=path, format="jsonc", ignore_list=True)

    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="widget", files=(file,), bundle_id=BUNDLE, config_dir=_spec())


def test_a_config_directory_preset_must_use_its_own_bundle_id() -> None:
    # when & then
    with pytest.raises(ConfigError):
        Preset.create(
            name="widget",
            files=(PresetFile.create(path=_spec().logical_path("x"), format="text"),),
            bundle_id="com.other.app",
            config_dir=_spec(),
        )


def test_a_file_outside_the_logical_root_is_refused() -> None:
    # when & then — written verbatim it would never go through the resolved folder
    with pytest.raises(ConfigError):
        _preset(paths=(f"{HOME}/Widget2026.2/options/a.xml",))


def test_deferring_while_running_needs_a_bundle_id() -> None:
    # when & then
    with pytest.raises(ConfigError):
        Preset.create(
            name="p", files=(PresetFile.create(path="a", format="text"),), bundle_id=None, defer_while_running=True
        )


def test_a_glob_inside_the_logical_root_is_accepted() -> None:
    # when
    preset = _preset(paths=(_spec().logical_path("options/colors/*.xml"),))

    # then
    assert [file.is_glob for file in preset.files] == [True]


# --------------------------------------------------------------------------- the catalog


def test_two_presets_sharing_a_layout_are_refused() -> None:
    # when & then
    with pytest.raises(ConfigError, match="share one config directory"):
        BuiltinCatalog.create((_preset(), _preset(name="widget-two", paths=(_spec().logical_path("other.xml"),))))


def test_a_preset_naming_a_real_release_path_under_a_logical_layout_is_refused() -> None:
    # given — another preset owns a path inside the real folder the logical one resolves to
    other = Preset.create(name="other", files=(PresetFile.create(path=f"{HOME}/Widget2026.2/x.xml", format="text"),))

    # when & then
    with pytest.raises(ConfigError, match="overlaps a logical config directory"):
        BuiltinCatalog.create((_preset(), other))


@pytest.mark.parametrize(
    ("path", "claimed"),
    [
        (f"{HOME}/@Widget/options/editor.xml", True),
        (f"{HOME}/@Widget/options/other.xml", False),
        (f"{HOME}/@widget/options/editor.xml", False),
        (f"{HOME}/@Widget", False),
        (f"{HOME}/Widget2026.2/options/editor.xml", False),
    ],
)
def test_the_catalog_owns_a_logical_path_and_reserves_the_rest_of_the_layout(path: str, claimed: bool) -> None:
    # given
    catalog = BuiltinCatalog.create((_preset(),))

    # when — a record naming the built-in preset, and one naming nobody, for the same path
    named, anonymous = catalog.knows("widget", path), catalog.knows("someone-else", path)

    # then — only an owned logical path is known; anything else on the layout is refused whatever the name says
    assert (named, anonymous) == (claimed, claimed)


def test_a_logical_path_takes_the_policy_and_preset_of_the_file_that_owns_it() -> None:
    # given
    catalog = BuiltinCatalog.create((_preset(),))
    path = f"{HOME}/@Widget/options/editor.xml"

    # when
    owner, policy = catalog.preset_for(path), catalog.policy_for(path)

    # then
    assert (None if owner is None else owner.name, None if policy is None else policy.path) == ("widget", path)
