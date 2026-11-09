# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared shape of a JetBrains IDE preset: one builder, so each product is a few lines of data.

Every JetBrains IDE keeps its settings in `~/Library/Application Support/JetBrains/<Product><year>.<n>/`, and the
files that matter share one set of names across products. The release folder changes with every major release, so a
record carries a **logical** path (`.../JetBrains/@DataGrip/options/editor-font.xml`, `core/config_dirs.py`) and the
folder is read from the installed app. A product module (`presets/datagrip.py`) holds that product's own evidence and
its extra files.

## What is tracked

Exact `options/` files, never an `options/*.xml` glob: a glob would sweep in whatever a plugin or a later release
writes there, including state that must not leave the machine. Each name below is the `@Storage` of a component whose
roaming type allows it to travel (intellij-community, `RoamingType.java`: `DISABLED` is local-only, `PER_OS` lives
under `options/mac/`):

```
options/editor-font.xml  console-font.xml  terminal-font.xml    fonts
options/colors.scheme.xml                                       which colour scheme is active
options/customization.xml  ide.general.xml  diff.xml            UI customization, general and diff settings
options/editor.xml                 EditorSettingsExternalizable   @Storage("editor.xml")
options/code.style.schemes.xml     PersistableCodeStyleSchemes    @Storage("code.style.schemes.xml")
options/laf.xml                    LafManagerImpl                 @Storage(value = "laf.xml", ...)
options/mac/keymap.xml             KeymapManagerImpl              @Storage("keymap.xml", PER_OS)
keymaps/*.xml  codestyles/*.xml  templates/*.xml  colors/*.icls   the user's own schemes (last element only)
```

The last four `options/` entries were **source-verified, not present on the Mac this was written on** (a product
folder that has never been customized has none of them). The `@Storage` lines were read in
`JetBrains/intellij-community` master on 2026-11-09. `find.xml` is **not** tracked: its `<mask>` elements are the
"recent file masks" history of the Find dialog, which changes with use like a recent-projects list.

## What is never tracked

Left out of the catalog (and, where it is sensitive, also blocked by name in `system/deny_list.py`, which is narrower
than this list: it names `dataSources.xml` and `dataSources.local.xml`, not every `dataSources*.xml`): every
`RoamingType.DISABLED` file (`window.state.xml`, `recentProjects.xml`, `usage.statistics.xml`,
`features.usage.statistics.xml`,
`trusted-paths.xml`, `path.macros.xml`, `updates.xml`, `ide.general.local.xml`, `security.xml`, `proxy.settings.xml`,
`certificates.xml`, `gitlab.xml`, `remote-servers.xml`, `settingsSyncLocal.xml`); `other.xml` and `window.layouts.xml`
(opaque state in CDATA); `jdk.table.xml` (absolute JDK paths); `databaseDrivers.xml`; `settingsSync*.xml`;
`github.xml`, `sshConfigs.xml`, `webServers.xml`; `dataSources*.xml` (connection URLs and user names); `*.key`,
`c.kdbx`, `c.pwd` (licence and credential stores); and `consoles/`, `workspace/`, `ssl/`, `jdbc-drivers/`.
`fileTemplates/` and `inspection/` are out of scope for now.

## Noise

* A colour scheme's `<metaInfo>` records `created`, `modified`, `ide` and `ideVersion` (the `META_INFO_*` names in
  `AbstractColorsScheme.java`): when and by which product the file was saved, so two Macs would disagree on every save.
  The **whole element** is stripped, not those four properties: a second `<metaInfo>` is a repeated group with no
  per-property path, and an empty `<metaInfo />` on one machine against a full one on another made apply refuse.
  Any other property inside `<metaInfo>` is therefore not synced either (none is known).
* `ide.general.xml`'s Registry holds `source="SYSTEM"` entries, which the IDE sets itself (`RegistryValueSource`). A key
  pattern matches a **key**, never an attribute value, so "every SYSTEM entry" cannot be written: only the one entry
  seen on a real install, `trace.state.event.service.url`, is stripped, and the others stay a known gap.

## Settings Sync

When the IDE's own Settings Sync is on (`options/settingsSync.xml`, read in memory by `system/app_config_dir.py`), the
product's files are neither collected nor applied, and `doctor` says so: two writers on one file would fight.
`syncEnabled` is compared case-insensitively. A missing file is off; a file that exists but cannot be read safely
(symlink, oversize, not UTF-8, broken XML, DOCTYPE) is unknown, and the product is skipped with "could not read
settingsSync.xml; leaving files alone", not read as off.

## Defer while running

An IDE rewrites a settings file when its component next saves (window focus loss, quit; non-roamable state every few
minutes), and nothing watches the file for outside edits, so a file written under a running IDE is overwritten. The
preset therefore sets `defer_while_running`: collect keeps flowing, and an apply that would change a file waits.
"""

from __future__ import annotations

from collections.abc import Iterable

from syncintosh.core.config_dirs import ConfigDirSpec
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.tree import KeyPattern

HOME = "Library/Application Support/JetBrains"
VENDOR = "JetBrains"

# Shown by `sync` and `doctor` when the IDE's own sync is on. It is the preset's data, so no service names a vendor.
SYNC_CONFLICT = "JetBrains Settings Sync is on - turn off one of the two"

# `options/` files every product writes, source-verified (see the module docstring).
COMMON_OPTIONS: tuple[str, ...] = (
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
    # PER_OS: the IDE keeps the active keymap's name per operating system, under `mac/`.
    "options/mac/keymap.xml",
)

# The user's own schemes: one directory each, last element only.
COMMON_GLOBS: tuple[tuple[str, str], ...] = (
    ("keymaps/*.xml", "xml"),
    ("codestyles/*.xml", "xml"),
    ("templates/*.xml", "xml"),
    ("colors/*.icls", "xml"),
)

# The whole `<metaInfo>` of a `ColorsScheme`, not its four `<property>` children one by one: it is when and by which IDE
# the scheme was saved. Naming the element also covers the shapes a per-property pattern misses: a second
# `<metaInfo>` (a repeated group, so no `property[...]` path exists under it) and an empty `<metaInfo />`
# (which one machine's file can have while another's is full, and apply then refused to re-attach it).
SCHEME_NOISE: frozenset[KeyPattern] = frozenset({("metaInfo",)})

# The one Registry entry known to be set by the IDE itself (`source="SYSTEM"`); see "Noise".
GENERAL_NOISE: frozenset[KeyPattern] = frozenset({("component[Registry]", "entry[trace.state.event.service.url]")})


def jetbrains_preset(
    name: str,
    prefix: str,
    bundle_id: str,
    cask: str,
    download_url: str,
    extra_options: Iterable[str] = (),
) -> Preset:
    """A JetBrains IDE preset: the common files, the product's `extra_options`, its config directory and its defer.

    `prefix` is the folder name before the release (`DataGrip`); `extra_options` are exact `options/` file names
    only this product writes (`options/databaseSettings.xml`).
    """
    spec = ConfigDirSpec.create(
        home=HOME, prefix=prefix, vendor=VENDOR, bundle_id=bundle_id, sync_conflict=SYNC_CONFLICT
    )
    exact = (*COMMON_OPTIONS, *extra_options)
    files = tuple(
        PresetFile.create(
            path=spec.logical_path(relative),
            format="xml",
            noise=GENERAL_NOISE if relative == "options/ide.general.xml" else (),
        )
        for relative in exact
    ) + tuple(
        PresetFile.create(
            path=spec.logical_path(relative),
            format="xml",
            noise=SCHEME_NOISE if relative.endswith(".icls") else (),
        )
        for relative, _ in COMMON_GLOBS
    )
    return Preset.create(
        name=name,
        files=files,
        bundle_id=bundle_id,
        download_url=download_url,
        cask=cask,
        config_dir=spec,
        defer_while_running=True,
    )


__all__ = [
    "COMMON_GLOBS",
    "COMMON_OPTIONS",
    "GENERAL_NOISE",
    "HOME",
    "SCHEME_NOISE",
    "SYNC_CONFLICT",
    "VENDOR",
    "jetbrains_preset",
]
