# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `zed` preset: its files, metadata, and the policy generated for each override layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import project_text, restore_text
from syncintosh.core.tree import ANY_KEY
from syncintosh.presets import BUILTIN_PRESETS, zed
from syncintosh.system.deny_list import is_denied

ADAPTER = JsoncAdapter()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zed"
SETTINGS: PresetFile = next(file for file in zed.ZED.files if file.path == zed.SETTINGS)


def test_the_zed_preset_names_its_files_and_is_built_in() -> None:
    # when
    paths = [file.path for file in zed.ZED.files]

    # then
    assert paths == [zed.SETTINGS, zed.KEYMAP, zed.SNIPPETS, zed.THEMES]
    assert zed.ZED in BUILTIN_PRESETS


def test_the_zed_preset_metadata_matches_the_verified_app() -> None:
    # when & then
    assert (zed.ZED.bundle_id, zed.ZED.cask, zed.ZED.download_url) == ("dev.zed.Zed", "zed", "https://zed.dev/download")


@pytest.mark.parametrize("path", [zed.KEYMAP, zed.SNIPPETS, zed.THEMES])
def test_the_files_other_than_settings_have_no_strip_list(path: str) -> None:
    # given
    file = next(file for file in zed.ZED.files if file.path == path)

    # when & then — the keymap is compared whole, and a snippet or a theme is the user's own text
    assert (file.stripped, file.arrays, file.ignore_list) == (frozenset(), (), False)


def test_the_snippet_and_theme_files_are_globs_and_the_keymap_is_not() -> None:
    # when
    globs = {file.path: file.is_glob for file in zed.ZED.files}

    # then
    assert globs == {zed.SETTINGS: False, zed.KEYMAP: False, zed.SNIPPETS: True, zed.THEMES: True}


def test_the_settings_file_has_no_ignore_list() -> None:
    # when & then — Zed has no Settings Sync, so there is no list to obey
    assert SETTINGS.ignore_list is False


@pytest.mark.parametrize("prefix", zed.PREFIXES, ids=lambda prefix: ".".join(str(part) for part in prefix) or "root")
def test_every_rule_is_generated_under_every_override_prefix(prefix) -> None:
    # when — the violations: a rule that is missing under this prefix
    missing = [
        rule
        for rules, held in ((zed.EXCLUDED_RULES, SETTINGS.excluded), (zed.NOISE_RULES, SETTINGS.noise))
        for rule in rules
        if (*prefix, *rule) not in held
    ]

    # then
    assert missing == []


@pytest.mark.parametrize(
    "prefix", [(), ("stable",), ("preview",), ("nightly",), ("dev",), ("macos",), ("linux",), ("windows",)]
)
def test_a_credential_is_cut_under_each_of_the_eight_literal_prefixes(prefix) -> None:
    # given — spelled out here, not taken from `zed.PREFIXES`, so a prefix missing from the preset is noticed
    layer = {
        "context_servers": {"s": {"command": "x", "env": {"K": "FAKE-SECRET"}}},
        "terminal": {"env": {"K": "FAKE"}},
        "language_models": {
            "openai_compatible": {"gw": {"api_url": "https://x.test", "custom_headers": {"X-Api-Key": "FAKE-OPAQUE"}}},
            "anthropic_compatible": {
                "px": {"api_url": "https://y.test", "custom_headers": {"X-Api-Key": "FAKE-OPAQUE"}}
            },
        },
    }
    document = {prefix[0]: layer} if prefix else layer

    # when
    sealed = project_text(ADAPTER, json.dumps(document), SETTINGS)

    # then
    assert "FAKE" not in sealed


def test_a_credential_is_cut_under_a_profile() -> None:
    # given
    settings = {
        "terminal": {"env": {"K": "FAKE-SECRET"}},
        "language_models": {
            "openai_compatible": {"gw": {"custom_headers": {"X-Api-Key": "FAKE-OPAQUE"}}},
            "anthropic_compatible": {"px": {"custom_headers": {"X-Api-Key": "FAKE-OPAQUE"}}},
        },
    }
    document = {"profiles": {"work": {"settings": settings}}}

    # when
    sealed = project_text(ADAPTER, json.dumps(document), SETTINGS)

    # then
    assert "FAKE" not in sealed


def test_the_nine_prefixes_are_the_root_channels_platforms_and_profiles() -> None:
    # when & then
    assert zed.PREFIXES == (
        (),
        ("stable",),
        ("preview",),
        ("nightly",),
        ("dev",),
        ("macos",),
        ("linux",),
        ("windows",),
        ("profiles", ANY_KEY, "settings"),
    )


def test_noise_and_excluded_do_not_overlap() -> None:
    # when & then
    assert SETTINGS.noise & SETTINGS.excluded == frozenset()


def test_the_font_sizes_are_synced_and_the_synced_credential_carriers_are_left_to_the_scan() -> None:
    # given — `persist` is false by default for the font-size actions, and these keys are reviewed by the scan
    synced = [
        ("buffer_font_size",),
        ("ui_font_size",),
        ("proxy",),
        ("lsp", ANY_KEY, "initialization_options"),
        ("lsp", ANY_KEY, "settings"),
        ("context_servers", ANY_KEY, "command"),
        ("context_servers", ANY_KEY, "args"),
    ]

    # when
    stripped = [rule for rule in synced if rule in SETTINGS.stripped]

    # then
    assert stripped == []


def test_the_development_credentials_file_is_denied(tmp_path: Path) -> None:
    # given
    home = tmp_path / "home"

    # when & then — and the settings beside it are not
    assert is_denied(home / ".config" / "zed" / "development_credentials", home) is True
    assert is_denied(home / ".config" / "zed" / "settings.json", home) is False


# --------------------------------------------------------------------------- the fixture


def test_the_settings_fixture_seals_no_credential_marker_at_any_layer() -> None:
    # given
    text = (FIXTURES / "settings.jsonc").read_text(encoding="utf-8")

    # when
    sealed = project_text(ADAPTER, text, SETTINGS)

    # then — every marker in the fixture is a credential, and none survives
    markers = [word for word in text.replace('"', " ").split() if word.startswith("FAKE-") and word.endswith("MARKER")]
    assert len(markers) >= 10
    assert [marker for marker in markers if marker in sealed] == []
    # the opaque header values are not scan-shaped, so only the exclusion keeps them out
    assert "fakeopaquetoken" not in sealed
    assert "https://gateway.example.test/v1" in sealed


def test_the_settings_fixture_keeps_what_is_synced() -> None:
    # given
    text = (FIXTURES / "settings.jsonc").read_text(encoding="utf-8")

    # when
    kept = project_text(ADAPTER, text, SETTINGS)

    # then — comments and the shared settings stay; the machine-local ones go
    assert "// Zed settings" in kept
    assert '"buffer_font_size": 15' in kept
    assert '"buffer_font_size": 17' in kept
    assert "@modelcontextprotocol/server-github" in kept
    assert "check" in kept
    assert "/usr/local/bin/node" not in kept
    assert 'rust-analyzer"' in kept
    assert "ra.log" not in kept
    assert "builder.example.test" not in kept
    assert '"font_size": 12' in kept


# --------------------------------------------------------------------------- properties

_LEAVES = st.none() | st.booleans() | st.integers() | st.text(max_size=5)
_NAMES = st.sampled_from(["a", "b", "*", "env"])
_NAME_SETS = st.lists(_NAMES, min_size=1, max_size=2, unique=True)
_INDENT = st.sampled_from([None, 2])


def _layer(names, shared, local) -> dict:
    return {
        "context_servers": {name: {"command": shared, "env": {"K": local}, "headers": {"H": local}} for name in names},
        "lsp": {name: {"binary": {"path": local, "arguments": [local]}, "settings": shared} for name in names},
        "terminal": {"shell": local, "env": {"K": local}, "font_size": shared},
        "node": {"path": local},
        "buffer_font_size": shared,
    }


def _document(names, shared, local, indent) -> str:
    value = {
        **_layer(names, shared, local),
        "preview": _layer(names, shared, local),
        "macos": _layer(names, shared, local),
        "profiles": {name: {"settings": _layer(names, shared, local)} for name in names},
    }
    return json.dumps(value, indent=indent)


@settings(deadline=None)
@given(names=_NAME_SETS, shared=_LEAVES, local=_LEAVES, indent=_INDENT)
def test_a_zed_projection_is_idempotent(names, shared, local, indent) -> None:
    # when
    once = project_text(ADAPTER, _document(names, shared, local, indent), SETTINGS)
    twice = project_text(ADAPTER, once, SETTINGS)

    # then
    assert twice == once


@settings(deadline=None)
@given(names=_NAME_SETS, shared=_LEAVES, before=_LEAVES, after=_LEAVES, indent=_INDENT)
def test_a_change_to_noise_or_a_credential_does_not_change_the_zed_projection(
    names, shared, before, after, indent
) -> None:
    # when — the documents differ only in values the policy strips
    first = project_text(ADAPTER, _document(names, shared, before, indent), SETTINGS)
    second = project_text(ADAPTER, _document(names, shared, after, indent), SETTINGS)

    # then
    assert first == second


@settings(deadline=None)
@given(names=_NAME_SETS, shared=_LEAVES, local=_LEAVES, indent=_INDENT)
def test_zed_restore_projects_back_to_the_repo_text(names, shared, local, indent) -> None:
    # given
    repo_text = project_text(ADAPTER, _document(names, shared, "sealed", indent), SETTINGS)
    live_text = _document(names, shared, local, indent)

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, SETTINGS)

    # then
    assert project_text(ADAPTER, restored, SETTINGS) == repo_text
