# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `datagrip` preset: the common JetBrains settings plus the database tool's own.

**Evidence: inspected on a real machine, 2026-11-09** (DataGrip 2026.2.6; no value was copied anywhere):

```
App:       DataGrip 2026.2.6, /Applications/DataGrip.app, build DB-262.10968.148
Bundle ID: com.jetbrains.datagrip
Metadata:  Contents/Resources/product-info.json: "dataDirectoryName": "DataGrip2026.2", "productVendor": "JetBrains"
Config:    ~/Library/Application Support/JetBrains/DataGrip2026.2/
           options/ consoles/ dataSourcesHistory/ jdbc-drivers/ ssl/ tasks/ workspace/ extensions/ grazie/ ...
           keymaps/ codestyles/ templates/ colors/ are absent (uncustomized)
           siblings consentOptions/ Daemon/ discovery/ are not IDE folders
Format:    options/*.xml: 34 files, no XML declaration, root <application>, 2-space indent, no trailing newline,
           empty elements written ` />`, CDATA in other.xml / window.layouts.xml / textmate.xml, attribute entities
           &#10; &#9; &lt; &quot; in databaseSettings.xml
Shapes:    component[@name]/option[@name][@value]; ide.general.xml component "Registry"/entry key value source;
           path.macros.xml macro name value; repeats with no identity: find.xml <mask> x12,
           databaseSettings.xml <parameter-pattern .../> x8, csvSettings.xml <set><option value/>...;
           databaseSettings.xml csv-format name id repeats;
           colors.scheme.xml <global_color_scheme name="Islands Dark" /> (the name is a value);
           settingsSync.xml has only migrationFromOldStorageChecked (sync off)
Writes:    lxml re-serialization turns ` />` into `/>` (confirmed), hence the minimal-edit XML adapter
Excluded:  no credential in a tracked file; dataSources*.xml, *.key, c.kdbx, c.pwd, security.xml, ssl/ never tracked
Download:  https://www.jetbrains.com/datagrip/download/   cask: datagrip (verified)
```

Sources (intellij-community): `PathManager.java`, `ProductProperties.kt`, `XPlatLauncher` `default.rs` (the folder
name), `ConfigImportHelper` (a new major release copies the newest previous folder of at most 180 days on first start),
`RoamingType.java` (`DISABLED` is local-only, `PER_OS` goes under `options/mac/`), `SettingsSyncSettings` (`syncEnabled`
defaults to false), and the absence of a VFS tracker for application-level storages: an outside edit is overwritten when
the component next saves (focus loss, exit; non-roamable state about every five minutes), hence `defer_while_running`.

The files, the noise and what is left out are in `presets/jetbrains.py`. DataGrip adds two `options/` files:
`databaseSettings.xml` (the settings of the database tool window, with no connection) and `csvSettings.xml`. A
connection (`dataSources.xml`, `dataSources.local.xml`) and a credential are never tracked.
"""

from __future__ import annotations

from syncintosh.presets.jetbrains import jetbrains_preset

DATAGRIP = jetbrains_preset(
    "datagrip",
    "DataGrip",
    "com.jetbrains.datagrip",
    "datagrip",
    "https://www.jetbrains.com/datagrip/download/",
    extra_options=("options/databaseSettings.xml", "options/csvSettings.xml"),
)

__all__ = ["DATAGRIP"]
