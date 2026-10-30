# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for profile logical paths: the codec, the layout, the catalog mapping, and the allowlist.

The VS Code layout used here is the shipped one (`presets/vscode.py`); its facts are source-verified
(issue #22), so no field name below is invented.
"""

from __future__ import annotations

import unicodedata
from typing import Literal
from urllib.parse import quote

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.config import LocalProfiles, SyncConfig, allows, allows_path
from syncintosh.core.errors import ConfigError
from syncintosh.core.manifest import ManifestRecord, validate_path
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import PresetCatalog, ProfileIndex
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.profiles import (
    PROFILE_MARK,
    ProfileEntry,
    ProfileFile,
    ProfileMap,
    ProfileSpec,
    decode_profile_name,
    duplicate_profile_names,
    encode_profile_name,
)
from syncintosh.presets import BuiltinCatalog, vscode

SPEC = vscode.PROFILES
HOME = vscode.PROFILE_HOME
CATALOG = BuiltinCatalog.default()
RECORD_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"

# Any text a profile can be called. Surrogates are not text, and VS Code stores its names as JSON.
NAMES = st.text(alphabet=st.characters(exclude_categories=["Cs"]), min_size=1, max_size=40)


def _config(names: Literal["*"] | tuple[str, ...] = "*") -> SyncConfig:
    entry = LocalProfiles.create(preset="vscode", spec=SPEC, scope="common", names=names)
    return SyncConfig.create(remote="r", machine_id="mac", items=(), profiles=(entry,))


def _record(path: str) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=RECORD_ID, path=path, preset="vscode", scope="common", machine=None, format="jsonc", sha256="a" * 64
    )


# --------------------------------------------------------------------------- codec


@pytest.mark.parametrize(
    ("name", "element"),
    [
        ("Work", "@Work"),
        ("a/b", "@a%2Fb"),
        ("a*b?c", "@a%2Ab%3Fc"),
        ("@home", "@%40home"),
        ("100%", "@100%25"),
        ("..", "@.."),
        ("café", "@caf%C3%A9"),
    ],
)
def test_a_name_encodes_to_one_safe_path_element(name: str, element: str) -> None:
    # when
    encoded = encode_profile_name(name)

    # then
    assert (encoded, decode_profile_name(encoded)) == (element, name)


@pytest.mark.parametrize("element", ["Work", "@", "@%2f", "@%41", "@%ff", "@a/b", "@%"])
def test_a_non_canonical_element_does_not_decode(element: str) -> None:
    # when
    name = decode_profile_name(element)

    # then
    assert name is None


@pytest.mark.parametrize("name", ["", "\ud800"])
def test_a_name_that_is_not_text_is_refused(name: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        encode_profile_name(name)


@given(NAMES)
def test_an_encoded_name_round_trips(name: str) -> None:
    # when
    element = encode_profile_name(name)

    # then
    assert (decode_profile_name(element), "/" in element) == (unicodedata.normalize("NFC", name), False)


@given(NAMES, st.sampled_from([f.path for f in SPEC.files if "*" not in f.path]))
def test_a_logical_path_is_a_valid_manifest_path_for_any_name(name: str, relative: str) -> None:
    # when
    path = SPEC.logical_path(name, relative)

    # then
    assert (validate_path(path), SPEC.parse(path).name) == (path, unicodedata.normalize("NFC", name))  # ty: ignore[unresolved-attribute]


@given(NAMES)
def test_two_spellings_of_a_name_fold_to_one_profile(name: str) -> None:
    # when
    repeated = duplicate_profile_names([name, name.upper().lower(), name.casefold()])

    # then
    assert repeated == (collision_key(name),)


@pytest.mark.parametrize("pair", [("Work", "work"), ("café", "café"), ("Straße", "STRASSE")])
def test_names_differing_in_case_or_normalization_are_one_profile(pair: tuple[str, str]) -> None:
    # when
    repeated = duplicate_profile_names(pair)

    # then
    assert len(repeated) == 1


# --------------------------------------------------------------------------- layout


@pytest.mark.parametrize(
    ("relative", "resource"),
    [
        ("settings.json", "settings"),
        ("keybindings.json", "keybindings"),
        ("snippets/python.json", "snippets"),
        ("snippets/team.code-snippets", "snippets"),
    ],
)
def test_a_logical_path_parses_to_its_profile_and_default_counterpart(relative: str, resource: str) -> None:
    # given
    path = SPEC.logical_path("My Work/Laptop", relative)

    # when
    parsed = SPEC.parse(path)

    # then
    assert parsed is not None
    assert (parsed.name, parsed.relative, parsed.resource, parsed.default_path) == (
        "My Work/Laptop",
        relative,
        resource,
        f"{vscode.PROFILE_ROOT}/{relative}",
    )


@pytest.mark.parametrize(
    "path",
    [
        f"{HOME}/-3f2a1b/settings.json",  # a raw id: owned by no preset
        f"{HOME}/3f2a1b/snippets/python.json",
        f"{HOME}/builtin/agents/settings.json",
        f"{HOME}/@Work",  # the directory, not a file
        f"{HOME}/@Work/tasks.json",  # a file the layout does not list
        f"{HOME}/@Work/extensions.json",
        f"{HOME}/@Work/snippets/*.json",  # the pattern text is not a file
        f"{HOME}/@Work/snippets/a/b.json",
        f"{HOME}/@wo%72k/settings.json",  # not the canonical encoding
        f"{HOME}/@/settings.json",
        f"{vscode.PROFILE_ROOT}/settings.json",  # the default profile is not a logical path
    ],
)
def test_a_path_that_is_not_a_logical_profile_path_does_not_parse(path: str) -> None:
    # when
    parsed = SPEC.parse(path)

    # then
    assert parsed is None


@pytest.mark.parametrize(
    "path",
    [
        f"{HOME.upper()}/@Work/settings.json",  # a home spelled in another case is not ours
        f"{HOME}/@Work/Settings.JSON",  # nor an exact file in another case
        f"{HOME}/@Work/snippets/a\x00.json",  # nor a name no manifest would accept
    ],
)
def test_a_non_canonical_spelling_does_not_parse(path: str) -> None:
    # when
    parsed = SPEC.parse(path)

    # then
    assert parsed is None


def test_a_snippet_glob_still_folds_the_file_name() -> None:
    # given
    path = f"{HOME}/@Work/snippets/Team.JSON"

    # when
    parsed = SPEC.parse(path)

    # then
    assert parsed is not None and parsed.relative == "snippets/Team.JSON"


_ONE_FILE = (ProfileFile.create(path="a.json", resource="r"),)


@pytest.mark.parametrize(
    ("home", "files"),
    [
        ("App/User/profiles", ()),
        ("App/User/profiles", (*_ONE_FILE, ProfileFile.create(path="A.json", resource="r"))),
        ("../profiles", _ONE_FILE),
    ],
)
def test_an_ill_formed_layout_is_refused(home: str, files: tuple[ProfileFile, ...]) -> None:
    # when & then
    with pytest.raises(ConfigError):
        ProfileSpec.create(default_root="App/User", home=home, index="App/User/storage.json", files=files)


def test_a_profile_file_without_a_resource_is_refused() -> None:
    # when & then
    with pytest.raises(ConfigError):
        ProfileFile.create(path="settings.json", resource="")


def test_a_profile_file_path_must_be_normalized() -> None:
    # when & then
    with pytest.raises(ConfigError):
        ProfileFile.create(path="../settings.json", resource="settings")


def test_a_preset_refuses_a_profile_file_it_has_no_default_for() -> None:
    # given — the spec lists a file the preset does not own, so no policy would be found for it
    spec = ProfileSpec.create(
        default_root="App/User",
        home="App/User/profiles",
        index="App/User/storage.json",
        files=(ProfileFile.create(path="tasks.json", resource="tasks"),),
    )
    owned = PresetFile.create(path="App/User/settings.json", format="jsonc")

    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="app", files=(owned,), profiles=spec)


# --------------------------------------------------------------------------- catalog


@pytest.mark.parametrize(
    "relative",
    [vscode.SETTINGS, vscode.KEYBINDINGS, f"{vscode.PROFILE_ROOT}/snippets/python.json"],
)
def test_a_profile_file_takes_its_default_counterparts_policy(relative: str) -> None:
    # given
    default = CATALOG.policy_for(relative)
    logical = SPEC.logical_path("Work", relative.removeprefix(f"{vscode.PROFILE_ROOT}/"))

    # when
    policy = CATALOG.policy_for(logical)

    # then
    assert policy is not None and policy == default


def test_a_profiles_settings_strip_the_same_keys_as_the_default_profiles() -> None:
    # when
    policy = CATALOG.policy_for(SPEC.logical_path("Work", "settings.json"))

    # then
    assert policy is not None
    assert (policy.noise, policy.excluded) == (vscode.NOISE, vscode.EXCLUDED)


def test_a_profiles_keybindings_keep_the_array_rule() -> None:
    # when
    policy = CATALOG.policy_for(SPEC.logical_path("Work", "keybindings.json"))

    # then
    assert policy is not None and [path for path, _ in policy.arrays] == [()]


@pytest.mark.parametrize("path", [f"{HOME}/-3f2a1b/settings.json", f"{HOME}/builtin/agents/settings.json"])
def test_a_raw_id_path_is_owned_by_no_preset(path: str) -> None:
    # when
    owners = (CATALOG.policy_for(path), CATALOG.knows("vscode", path), CATALOG.preset_for(path))

    # then
    assert owners == (None, False, None)


def test_the_catalog_knows_a_logical_path_for_its_own_preset_only() -> None:
    # given
    path = SPEC.logical_path("Work", "settings.json")

    # when
    results = (CATALOG.knows("vscode", path), CATALOG.knows("zsh", path), CATALOG.knows("community", path))

    # then — a built-in name must own the path; a name that is not built in makes no claim
    assert results == (True, False, True)


def test_a_logical_path_reports_its_preset_for_a_deferral() -> None:
    # when
    preset = CATALOG.preset_for(SPEC.logical_path("Work", "snippets/go.code-snippets"))

    # then
    assert preset is not None and preset.name == "vscode"


def test_the_catalog_still_satisfies_the_port() -> None:
    # when & then
    assert isinstance(CATALOG, PresetCatalog)


# --------------------------------------------------------------------------- allowlist


def test_a_star_entry_allows_any_profile_name() -> None:
    # given
    config = _config("*")

    # when
    allowed = [
        allows_path(config, scope="common", machine=None, path=SPEC.logical_path(n, "settings.json"))
        for n in ("A", "b/c")
    ]

    # then
    assert allowed == [True, True]


def test_a_named_entry_allows_only_those_names_folded() -> None:
    # given
    config = _config(("Work",))

    # when
    allowed = [
        allows_path(config, scope="common", machine=None, path=SPEC.logical_path(name, "settings.json"))
        for name in ("Work", "work", "Play")
    ]

    # then
    assert allowed == [True, True, False]


@pytest.mark.parametrize(
    "path",
    [
        f"{HOME}/-3f2a1b/settings.json",
        f"{HOME}/@Work/tasks.json",
        f"{vscode.PROFILE_ROOT}/settings.json",
    ],
)
def test_the_allowlist_refuses_a_raw_id_and_an_unlisted_file(path: str) -> None:
    # given
    config = _config("*")

    # when
    allowed = allows(config, _record(path))

    # then
    assert allowed is False


def test_a_profile_entry_does_not_admit_another_scope() -> None:
    # given
    config = _config("*")
    path = SPEC.logical_path("Work", "settings.json")

    # when
    allowed = allows_path(config, scope="machine", machine="mac", path=path)

    # then
    assert allowed is False


def test_a_config_without_profiles_refuses_a_logical_path() -> None:
    # given — an existing config has no `profiles` entry, so behavior is unchanged
    config = SyncConfig.create(remote="r", machine_id="mac", items=())

    # when
    allowed = allows_path(config, scope="common", machine=None, path=SPEC.logical_path("Work", "settings.json"))

    # then
    assert allowed is False


@pytest.mark.parametrize("names", [("Work", "work"), ("",), "Work"])
def test_ill_formed_names_are_refused(names) -> None:
    # when & then
    with pytest.raises(ConfigError):
        LocalProfiles.create(preset="vscode", spec=SPEC, scope="common", names=names)


def test_a_machine_scope_profile_entry_must_name_this_machine() -> None:
    # given
    entry = LocalProfiles.create(preset="vscode", spec=SPEC, scope="machine", machine="other", names="*")

    # when & then
    with pytest.raises(ConfigError):
        SyncConfig.create(remote="r", machine_id="mac", items=(), profiles=(entry,))


@given(NAMES, st.sampled_from(["settings.json", "keybindings.json", "snippets/a.json", "snippets/a.code-snippets"]))
def test_every_logical_path_of_an_allowed_name_is_allowed_and_owned(name: str, relative: str) -> None:
    # given
    config = _config((name,))
    path = SPEC.logical_path(name, relative)

    # when
    verdict = (allows_path(config, scope="common", machine=None, path=path), CATALOG.knows("vscode", path))

    # then
    assert verdict == (True, True)


# --------------------------------------------------------------------------- resolved map


def test_a_profile_map_finds_a_name_by_its_folded_key() -> None:
    # given
    entry = ProfileEntry.create(name="Work", directory="-3f2a1b", borrowed=["keybindings"])
    profiles = ProfileMap.create((entry,))

    # when
    found = (profiles.lookup("WORK"), profiles.lookup("Play"))

    # then
    assert found == (entry, None)


@pytest.mark.parametrize("directory", ["builtin/agents", "..", "", "ABC", "a b", "/etc", "-"])
def test_a_directory_that_is_not_a_vscode_id_is_refused(directory: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        ProfileEntry.create(name="Work", directory=directory)


def test_two_profiles_with_one_folded_name_are_refused() -> None:
    # given
    entries = (ProfileEntry.create(name="Work", directory="a1"), ProfileEntry.create(name="work", directory="b2"))

    # when & then
    with pytest.raises(ConfigError):
        ProfileMap.create(entries)


def test_an_unreadable_index_resolves_no_profile_and_says_why() -> None:
    # when
    profiles = ProfileMap.unreadable("storage.json is corrupt")

    # then
    assert (profiles.readable, profiles.entries, profiles.problems, profiles.lookup("Work")) == (
        False,
        (),
        ("storage.json is corrupt",),
        None,
    )


def test_the_profile_index_port_is_structural() -> None:
    # given
    class FakeIndex:
        def read(self, spec: ProfileSpec) -> ProfileMap:
            return ProfileMap.create(())

    # when
    matches = isinstance(FakeIndex(), ProfileIndex)

    # then
    assert matches is True


def test_two_names_on_one_directory_are_refused() -> None:
    # given
    entries = (ProfileEntry.create(name="A", directory="a1"), ProfileEntry.create(name="B", directory="a1"))

    # when & then
    with pytest.raises(ConfigError):
        ProfileMap.create(entries)


def test_borrowed_resources_must_be_a_list_not_a_string() -> None:
    # when & then
    with pytest.raises(ConfigError):
        ProfileEntry.create(name="Work", directory="a1", borrowed="settings")


def test_a_profiles_entry_needs_a_layout() -> None:
    # when & then
    with pytest.raises(ConfigError):
        LocalProfiles.create(preset="vscode", spec="nope", scope="common")  # ty: ignore[invalid-argument-type]  # the guard under test


def test_a_preset_refuses_to_track_its_profile_index() -> None:
    # given
    spec = ProfileSpec.create(
        default_root="App/User",
        home="App/User/profiles",
        index="App/User/settings.json",
        files=(ProfileFile.create(path="settings.json", resource="settings"),),
    )
    owned = PresetFile.create(path="App/User/settings.json", format="jsonc")

    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="app", files=(owned,), profiles=spec)


def test_a_preset_refuses_a_glob_that_covers_its_profile_index() -> None:
    # given
    spec = ProfileSpec.create(
        default_root="App/User",
        home="App/User/profiles",
        index="App/User/globalStorage/storage.json",
        files=(ProfileFile.create(path="settings.json", resource="settings"),),
    )
    files = (
        PresetFile.create(path="App/User/settings.json", format="jsonc"),
        PresetFile.create(path="App/User/globalStorage/*.json", format="jsonc"),
    )

    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="app", files=files, profiles=spec)


# --------------------------------------------------------------------------- NFC


@given(NAMES)
def test_nfc_and_nfd_spellings_encode_to_one_element(name: str) -> None:
    # when
    nfc, nfd = (
        encode_profile_name(unicodedata.normalize("NFC", name)),
        encode_profile_name(unicodedata.normalize("NFD", name)),
    )

    # then
    assert nfc == nfd


@pytest.mark.parametrize("name", ["Café", "작업"])
def test_an_nfd_encoded_element_is_not_a_logical_path(name: str) -> None:
    # given — the element a build that did not normalize would have written for the decomposed spelling
    nfd = unicodedata.normalize("NFD", name)
    element = PROFILE_MARK + quote(nfd, safe="")
    path = f"{HOME}/{element}/settings.json"

    # when
    verdict = (decode_profile_name(element), SPEC.parse(path), CATALOG.knows("vscode", path))

    # then
    assert verdict == (None, None, False)


@pytest.mark.parametrize("name", ["Café", "작업"])
def test_both_spellings_of_a_name_give_one_logical_path(name: str) -> None:
    # when
    paths = {SPEC.logical_path(unicodedata.normalize(form, name), "settings.json") for form in ("NFC", "NFD")}

    # then
    assert len(paths) == 1


# --------------------------------------------------------------------------- one entry per preset


def _other_spec() -> ProfileSpec:
    return ProfileSpec.create(
        default_root="App/User",
        home="App/User/profiles",
        index="App/User/storage.json",
        files=(ProfileFile.create(path="settings.json", resource="settings"),),
    )


def test_two_profiles_entries_for_one_preset_are_refused() -> None:
    # given
    common = LocalProfiles.create(preset="vscode", spec=SPEC, scope="common", names="*")
    machine = LocalProfiles.create(preset="vscode", spec=SPEC, scope="machine", machine="mac", names=("Work",))

    # when & then
    with pytest.raises(ConfigError, match="vscode"):
        SyncConfig.create(remote="r", machine_id="mac", items=(), profiles=(common, machine))


def test_profiles_entries_for_different_presets_are_allowed() -> None:
    # given
    first = LocalProfiles.create(preset="vscode", spec=SPEC, scope="common", names="*")
    second = LocalProfiles.create(preset="app", spec=_other_spec(), scope="common", names="*")

    # when
    config = SyncConfig.create(remote="r", machine_id="mac", items=(), profiles=(first, second))

    # then
    assert [entry.preset for entry in config.profiles] == ["vscode", "app"]
