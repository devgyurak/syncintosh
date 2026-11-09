# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `datagrip` preset and the JetBrains builder: files, metadata, noise, and what is never tracked."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.xml import XmlAdapter
from syncintosh.core.config_dirs import ConfigDirSpec
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import project_text, restore_text
from syncintosh.presets import BUILTIN_PRESETS, BuiltinCatalog, datagrip, jetbrains
from syncintosh.presets.jetbrains import jetbrains_preset
from syncintosh.system.deny_list import is_denied

ADAPTER = XmlAdapter()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jetbrains"
SPEC = datagrip.DATAGRIP.config_dir
ROOT = "Library/Application Support/JetBrains/@DataGrip"
CATALOG = BuiltinCatalog.default()


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _policy(relative: str) -> PresetFile:
    policy = CATALOG.policy_for(f"{ROOT}/{relative}")
    assert policy is not None
    return policy


EXACT = [
    "options/editor-font.xml",
    "options/console-font.xml",
    "options/terminal-font.xml",
    "options/colors.scheme.xml",
    "options/customization.xml",
    "options/ide.general.xml",
    "options/diff.xml",
    "options/editor.xml",
    "options/code.style.schemes.xml",
    "options/laf.xml",
    "options/mac/keymap.xml",
    "options/databaseSettings.xml",
    "options/csvSettings.xml",
]
GLOBS = ["keymaps/*.xml", "codestyles/*.xml", "templates/*.xml", "colors/*.icls"]


def test_the_datagrip_preset_names_exactly_these_files_and_is_built_in() -> None:
    # when
    paths = [file.path for file in datagrip.DATAGRIP.files]

    # then
    assert paths == [f"{ROOT}/{relative}" for relative in (*EXACT, *GLOBS)]
    assert datagrip.DATAGRIP in BUILTIN_PRESETS


def test_the_datagrip_metadata_matches_the_inspected_app() -> None:
    # when & then
    assert (datagrip.DATAGRIP.bundle_id, datagrip.DATAGRIP.cask, datagrip.DATAGRIP.download_url) == (
        "com.jetbrains.datagrip",
        "datagrip",
        "https://www.jetbrains.com/datagrip/download/",
    )
    assert datagrip.DATAGRIP.defer_while_running is True
    assert (
        ConfigDirSpec.create(
            home=jetbrains.HOME,
            prefix="DataGrip",
            vendor="JetBrains",
            bundle_id="com.jetbrains.datagrip",
            sync_conflict=jetbrains.SYNC_CONFLICT,
        )
        == SPEC
    )


def test_every_file_is_xml_and_only_the_last_four_are_globs() -> None:
    # when
    shape = [(file.format, file.is_glob) for file in datagrip.DATAGRIP.files]

    # then
    assert shape == [("xml", False)] * len(EXACT) + [("xml", True)] * len(GLOBS)


@pytest.mark.parametrize(
    "never",
    [
        "options/window.state.xml",
        "options/recentProjects.xml",
        "options/usage.statistics.xml",
        "options/features.usage.statistics.xml",
        "options/trusted-paths.xml",
        "options/path.macros.xml",
        "options/updates.xml",
        "options/ide.general.local.xml",
        "options/security.xml",
        "options/proxy.settings.xml",
        "options/certificates.xml",
        "options/gitlab.xml",
        "options/remote-servers.xml",
        "options/settingsSyncLocal.xml",
        "options/settingsSync.xml",
        "options/other.xml",
        "options/window.layouts.xml",
        "options/jdk.table.xml",
        "options/databaseDrivers.xml",
        "options/github.xml",
        "options/sshConfigs.xml",
        "options/webServers.xml",
        "options/dataSources.xml",
        "options/dataSources.local.xml",
        "options/find.xml",
        "DataGrip.key",
        "c.kdbx",
        "c.pwd",
        "consoles/db/console.sql",
        "workspace/x.xml",
        "ssl/ca.pem",
    ],
)
def test_a_file_that_must_never_travel_is_owned_by_no_policy(never: str) -> None:
    # when
    policy = CATALOG.policy_for(f"{ROOT}/{never}")

    # then
    assert policy is None


@pytest.mark.parametrize(
    "denied",
    [
        "options/dataSources.xml",
        "options/dataSources.local.xml",
        "options/security.xml",
        "DataGrip.key",
        "c.kdbx",
        "c.pwd",
        "ssl/ca.pem",
        "consoles/db/x.sql",
    ],
)
def test_the_sensitive_ones_are_also_blocked_in_every_release_folder(tmp_path: Path, denied: str) -> None:
    # when
    blocked = is_denied(tmp_path / "Library/Application Support/JetBrains/DataGrip2026.3" / denied, tmp_path)

    # then
    assert blocked is True


def test_the_scheme_noise_is_the_four_meta_info_properties() -> None:
    # when
    noise = _policy("colors/sample.icls").noise

    # then
    assert noise == {("metaInfo",)}


@pytest.mark.parametrize(
    ("relative", "fixture", "gone", "kept"),
    [
        (
            "colors/sample.icls",
            "sample.icls",
            ("2026-01-01T10:00:00", "2026.2.6.0", "DataGrip<"),
            ("ABCDEF", "Sample Scheme"),
        ),
        (
            "options/ide.general.xml",
            "ide.general.xml",
            ("trace.state.event.service.url",),
            ("ide.sample.flag", "showTipsOnStartup"),
        ),
    ],
)
def test_the_noise_is_cut_from_what_is_sealed_and_the_rest_stays(
    relative: str, fixture: str, gone: tuple[str, ...], kept: tuple[str, ...]
) -> None:
    # given
    policy = _policy(relative)

    # when
    projected = project_text(ADAPTER, _text(fixture), policy)

    # then
    assert [text for text in gone if text in projected] == []
    assert [text for text in kept if text not in projected] == []


@pytest.mark.parametrize("fixture", ["databaseSettings.xml", "editor-font.xml", "colors.scheme.xml", "keymap.xml"])
def test_a_file_without_noise_is_sealed_as_it_is(fixture: str) -> None:
    # given
    policy = _policy({"keymap.xml": "options/mac/keymap.xml"}.get(fixture, f"options/{fixture}"))

    # when
    projected = project_text(ADAPTER, _text(fixture), policy)

    # then
    assert projected == _text(fixture)


_WORD = st.text(alphabet="abcdefghij0123456789-:.", min_size=1, max_size=12)


def _scheme(created: str, modified: str, ide: str, version: str, color: str) -> str:
    return (
        '<scheme name="S" version="142" parent_scheme="Darcula">\n'
        "  <metaInfo>\n"
        f'    <property name="created">{created}</property>\n'
        f'    <property name="modified">{modified}</property>\n'
        f'    <property name="ide">{ide}</property>\n'
        f'    <property name="ideVersion">{version}</property>\n'
        "  </metaInfo>\n"
        f'  <colors>\n    <option name="CARET_COLOR" value="{color}" />\n  </colors>\n'
        "</scheme>"
    )


@given(first=st.tuples(_WORD, _WORD, _WORD, _WORD), second=st.tuples(_WORD, _WORD, _WORD, _WORD), color=_WORD)
def test_a_noise_only_change_leaves_the_projection_identical(
    first: tuple[str, str, str, str], second: tuple[str, str, str, str], color: str
) -> None:
    # given
    policy = _policy("colors/sample.icls")

    # when
    one = project_text(ADAPTER, _scheme(*first, color), policy)
    other = project_text(ADAPTER, _scheme(*second, color), policy)

    # then
    assert one == other


@given(repo_meta=st.tuples(_WORD, _WORD, _WORD, _WORD), live_meta=st.tuples(_WORD, _WORD, _WORD, _WORD), color=_WORD)
def test_projecting_a_restored_file_gives_back_the_repo_object(
    repo_meta: tuple[str, str, str, str], live_meta: tuple[str, str, str, str], color: str
) -> None:
    # given — the repo object is a projection; the live file carries this machine's own metadata
    policy = _policy("colors/sample.icls")
    repo = project_text(ADAPTER, _scheme(*repo_meta, color), policy)
    live = _scheme(*live_meta, color)

    # when
    restored = restore_text(ADAPTER, repo, live, policy)

    # then
    assert project_text(ADAPTER, restored, policy) == repo


@given(url=_WORD, other=_WORD)
def test_a_registry_noise_change_does_not_move_the_projection(url: str, other: str) -> None:
    # given
    policy = _policy("options/ide.general.xml")
    template = _text("ide.general.xml")

    # when
    one = project_text(ADAPTER, template.replace("https://example.invalid/a", f"https://{url}.invalid"), policy)
    two = project_text(ADAPTER, template.replace("https://example.invalid/a", f"https://{other}.invalid"), policy)

    # then
    assert one == two


def test_the_catalog_refuses_a_second_preset_on_the_same_config_directory() -> None:
    # given
    twin = jetbrains_preset("twin", "DataGrip", "com.example.twin", "twin", "https://example.com/twin")

    # when & then
    with pytest.raises(Exception, match="claim one path"):
        BuiltinCatalog.create((datagrip.DATAGRIP, twin))


def test_the_builder_adds_a_products_extra_options_after_the_common_ones() -> None:
    # given
    other = jetbrains_preset(
        "other", "Other", "com.example.other", "other", "https://example.com/o", extra_options=("options/x.xml",)
    )
    layout = ConfigDirSpec.create(
        home=jetbrains.HOME,
        prefix="Other",
        vendor=jetbrains.VENDOR,
        bundle_id="com.example.other",
        sync_conflict=jetbrains.SYNC_CONFLICT,
    )

    # when
    paths = [file.path for file in other.files]

    # then
    assert paths.index(layout.logical_path("options/x.xml")) == len(jetbrains.COMMON_OPTIONS)
    assert other.config_dir == layout


_SCHEME = '<scheme name="S" version="142">\n{meta}  <colors>\n    <option name="C" value="1" />\n  </colors>\n</scheme>'
_META = (
    '  <metaInfo>\n    <property name="created">{n}</property>\n    <property name="ide">X</property>\n  </metaInfo>\n'
)
_FILLED = _SCHEME.format(meta=_META.format(n=1))
_BARE = _SCHEME.format(meta="")
_METAS = {
    "filled": _FILLED,
    "twice": _SCHEME.format(meta=_META.format(n=1) + _META.format(n=2)),
    "empty": _SCHEME.format(meta="  <metaInfo />\n"),
    "empty-then-filled": _SCHEME.format(meta="  <metaInfo />\n" + _META.format(n=1)),
}


@pytest.mark.parametrize("shape", sorted(_METAS))
def test_every_shape_of_metainfo_is_stripped(shape: str) -> None:
    # given
    policy = _policy("colors/sample.icls")

    # when
    projected = project_text(ADAPTER, _METAS[shape], policy)

    # then
    assert projected == _BARE


@pytest.mark.parametrize("repo_shape", sorted(_METAS))
@pytest.mark.parametrize("live_shape", sorted(_METAS))
def test_apply_restores_any_metainfo_shape_against_any_other(repo_shape: str, live_shape: str) -> None:
    # given
    policy = _policy("colors/sample.icls")
    repo = project_text(ADAPTER, _METAS[repo_shape], policy)

    # when
    restored = restore_text(ADAPTER, repo, _METAS[live_shape], policy)

    # then
    assert project_text(ADAPTER, restored, policy) == repo
