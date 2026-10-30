# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `vscode` preset: `settings.json` and `keybindings.json`, source-verified.

**Evidence: verified against the source; not inspected on a real machine** (the user approved
that exception for this preset — decision V4 of issue #6). VS Code is not installed on the
machine this was written on, so every value below comes from the code and the vendor's own
documentation rather than from a local file:

```
App:       Visual Studio Code (stable)
Bundle ID: com.microsoft.VSCode        (the Homebrew cask's zap paths)
Paths:     ~/Library/Application Support/Code/User/settings.json    (JSONC object)
           ~/Library/Application Support/Code/User/keybindings.json (JSONC array, merged per binding)
           ~/Library/Application Support/Code/User/snippets/*.json          (language snippets)
           ~/Library/Application Support/Code/User/snippets/*.code-snippets (global snippets)
Format:    JSONC (comments and trailing commas; `setProperty` edits preserve both)
Noise:     the keys `userDataSync` computes as "not synced" — see below
Excluded:  http.proxyAuthorization
Download:  https://code.visualstudio.com/download      cask: visual-studio-code
```

Sources:

- `microsoft/vscode` `main`: `src/vs/platform/userDataSync/common/userDataSync.ts` —
  `getIgnoredSettings`, which is what makes a setting "not synced":
  `properties[key].ignoreSync || scope === MACHINE || scope === MACHINE_OVERRIDABLE`. There is no
  hard-coded list any more; the set is computed from each setting's registration, which is why
  this preset's list is the setting ids that registration marks.
- `microsoft/vscode` `main`: `src/vs/workbench/services/configuration/common/configurationEditing.ts`
  (`setProperty` rewrites only the value's span, so comments survive) and
  `src/vs/platform/files/node/diskFileSystemProvider.ts` (an atomic `.vsctmp` + rename save, so a
  copy-based collect never reads a half-written file).
- `microsoft/vscode` `src/vs/platform/userDataSync/common/keybindingsMerge.ts` at commit
  `6b924c51528e663dda5091a1493229a361676aca` (the file's upstream history, read for issue #21). Its
  conflict unit is the **key**: two machines that bind different commands to one key conflict there,
  where this preset merges per `key` + `when` group. Its `-command` removals match by command name alone,
  where this preset keeps a removal as an element identified by `key`, `command` and `when`. Its key parser
  lowercases keys, where this preset compares them exactly. These three are deliberate differences, and the
  preset's `array_identity` rule records them; the design docs carry the reasoning.
- `code.visualstudio.com/docs/configure/settings` and `brew info --cask visual-studio-code`.
- `code.visualstudio.com/docs/editor/userdefinedsnippets` — "Create your own snippets": user
  snippets live under `Code/User/snippets/`, a language snippet file is `javascript.json`, the files
  are "written in JSON, support C-style comments", and a global snippet file is "JSON with the file
  suffix `.code-snippets`". That is two globs rather than exact paths: which snippet files exist is a
  per-machine fact, so this preset names the directory and the two suffixes.

## What is noise, and why it is safe to name a key that never matches

A `noise` entry is a **match** against the file: a key that is not there is simply not stripped.
So a stale entry is a no-op, while a *missing* one is churn on every sync — the asymmetry is why
the list errs towards completeness, and why the fixture only carries keys whose names the
research confirmed. `terminal.integrated.*` is deliberately **absent**: upstream syncs it, so
this preset does too (decision V1).

**The same reasoning does not extend to `excluded`.** A `noise` typo costs nothing; an `excluded`
typo means the credential is not cut, and `http.proxyAuthorization`'s value is a literal
`Basic <base64>`, which the secret scan cannot classify. So both spellings are registered rather
than the one that looks canonical today.

`keybindings.json` has no strip list at all, which leaves the secret scan as the only gate for it:
a credential shape the scan does not know would be sealed. That is the accepted limit of a
heuristic backstop (``AGENTS.md`` §6), and the reason the `scheme://user:pass@` pattern was added
with this preset.

## Profiles (issue #22)

A non-default profile lives under `User/profiles/<id>/`, and `<id>` is `hash(generateUuid()).toString(16)` —
a signed int32 that may start with `-` and differs between Macs. Records therefore carry a logical path
(`…/profiles/@<name>/<file>`, `core/profiles.py`) and the real directory is looked up on each machine from
`User/globalStorage/storage.json`, key `userDataProfiles`, entries `{name, location, icon?, useDefaultFlags?}`.
Sources (`microsoft/vscode` `main` `a48dd23e2ac5`): `src/vs/platform/userDataProfile/common/userDataProfile.ts`,
`.../node/userDataProfile.ts`, `userDataProfilesManifestSync.ts`, `userDataProfilesManifestMerge.ts`,
`state/node/stateService.ts`, and vscode-docs `configure/profiles.md`. Not inspected on a real machine (the
approved exception, as above). `read_profiles` reads only that key; `storage.json` is never collected.

## What is out of scope

- `settingsSync.ignoredSettings` is not consulted: reproducing VS Code's own ignore list would
  mean reading a setting the sync never applies, and it is a follow-up (issue #6, V3).
- A snippet file is synced as ordinary JSONC with no strip list, and the secret scan is the backstop
  for a credential inlined in a `body`. `tasks.json`, `mcp.json`, `extensions.json`, `prompts/`, Insiders,
  portable mode, `--user-data-dir`, and project snippets (`<project>/.vscode/*.code-snippets`, which are not under
  the `$HOME` user directory) are not tracked. `mcp.json` in particular holds server `env` and
  `headers`, which are credentials, and it needs its own exclusion pass before it can be collected
  safely.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from syncintosh.core.errors import ConfigError
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.profiles import (
    PROFILE_ID,
    ProfileEntry,
    ProfileFile,
    ProfileMap,
    ProfileSpec,
    duplicate_profile_names,
    encode_profile_name,
    profile_name_key,
)
from syncintosh.core.tree import KeyPath

SETTINGS = "Library/Application Support/Code/User/settings.json"
KEYBINDINGS = "Library/Application Support/Code/User/keybindings.json"
SNIPPETS_JSON = "Library/Application Support/Code/User/snippets/*.json"
SNIPPETS_GLOBAL = "Library/Application Support/Code/User/snippets/*.code-snippets"

# Where VS Code keeps non-default profiles and the list of them. The list is read in memory for its
# `userDataProfiles` key only; the file also holds window state and a telemetry machine id, and is never
# collected (issue #22). `resource` is the name `useDefaultFlags` uses for what a profile may borrow.
PROFILE_ROOT = "Library/Application Support/Code/User"
PROFILE_HOME = f"{PROFILE_ROOT}/profiles"
PROFILE_INDEX = f"{PROFILE_ROOT}/globalStorage/storage.json"
PROFILES = ProfileSpec.create(
    default_root=PROFILE_ROOT,
    home=PROFILE_HOME,
    index=PROFILE_INDEX,
    files=(
        ProfileFile.create(path="settings.json", resource="settings"),
        ProfileFile.create(path="keybindings.json", resource="keybindings"),
        ProfileFile.create(path="snippets/*.json", resource="snippets"),
        ProfileFile.create(path="snippets/*.code-snippets", resource="snippets"),
    ),
)

# Machine-local settings: the ones `userDataSync.getIgnoredSettings` excludes (a registration with
# `ignoreSync`, or `scope: MACHINE` / `MACHINE_OVERRIDABLE`). They are cut on the collect side, so
# a changed zoom level or editor path produces neither a diff nor a conflict, and each machine
# keeps its own value.
NOISE: frozenset[KeyPath] = frozenset(
    {
        # The window's own geometry: written per machine, and ignored by Settings Sync.
        ("window.zoomLevel",),
        ("keyboard.layout",),
        # An absolute path to wherever this machine keeps its projects and dialogs.
        ("files.dialog.defaultPath",),
        ("workbench.welcomePage.walkthroughs.openOnInstall",),
        ("http.useLocalProxyConfiguration",),
        # A git binary and clone directory per machine, and two "don't warn me again" flags whose
        # answer is a user's local choice.
        ("git.path",),
        ("git.defaultCloneDirectory",),
        ("git.ignoreMissingGitWarning",),
        ("git.ignoreLegacyWarning",),
        # Package managers, tsserver plugin directories, and profiler output: absolute paths.
        ("typescript.npm",),
        ("typescript.tsserver.pluginPaths",),
        ("js/ts.tsserver.npm.path",),
        ("js/ts.tsserver.diagnosticDir",),
        ("js/ts.tsserver.heapProfile",),
        ("js/ts.tsserver.pluginPaths",),
        ("php.validate.executablePath",),
        ("emmet.extensionsPath",),
    }
)

# Credentials. `http.proxyAuthorization` is how you hand VS Code a proxy's credentials, and the
# value is typically a literal `Basic <base64>`. Registered in **both** shapes a file can spell it
# in — the dotted key VS Code writes, and a nested object — so neither spelling can carry a
# credential into the repo (decision V2).
EXCLUDED: frozenset[KeyPath] = frozenset({("http.proxyAuthorization",), ("http", "proxyAuthorization")})

VSCODE = Preset.create(
    name="vscode",
    files=(
        PresetFile.create(path=SETTINGS, format="jsonc", noise=NOISE, excluded=EXCLUDED),
        # No strip list, but the bindings merge one at a time. The array's order carries meaning (a later
        # entry overrides an earlier one), so the merge keeps local's order and places each addition after
        # its predecessor on its own side, and it never reorders across sides silently. Two bindings are
        # the same group when they share `key` and `when`; a group both machines changed differently is a
        # conflict, and only that binding is asked about. The conflict names a binding by its key and a digest of
        # its `when`, never the text; its command and args are never printed. `key` is compared exactly, so
        # `Ctrl+A` and `ctrl+a` are two groups even though VS Code reads them as one (decision V6, issue #21).
        PresetFile.create(
            path=KEYBINDINGS,
            format="jsonc",
            array_identity={
                (): ArrayRule.create(
                    identity=("key", "command", "when"), required=("key", "command"), group=("key", "when")
                ),
            },
        ),
        # User snippets: a **directory** of per-machine files, so a glob rather than an exact path.
        # The two patterns do not overlap — one file is either a language `.json` snippet or a global
        # `.code-snippets` one — and a file the other machine created is written here only because
        # this machine's own config names the same glob (`core/config.LocalGlob`). No strip list: a
        # snippet is the user's own text, and the secret scan is the backstop for a token inlined in
        # a `body`.
        PresetFile.create(path=SNIPPETS_JSON, format="jsonc"),
        PresetFile.create(path=SNIPPETS_GLOBAL, format="jsonc"),
    ),
    bundle_id="com.microsoft.VSCode",
    download_url="https://code.visualstudio.com/download",
    cask="visual-studio-code",
    profiles=PROFILES,
)

_PROFILES_KEY = "userDataProfiles"
# The suffix an old-format `location` URI's `path` must end in. The directory names are VS Code's own
# (`User`, `profiles`); the id after them is checked like a new-format `location`.
_URI_PARENT = ("User", "profiles")


def _directory_of(location: object) -> str | None:
    """The profile's directory id from a `location`, or `None` when it is not a single-element id.

    Current VS Code writes `location` as a string relative to `profiles/`, which is the id itself. An older
    build wrote a URI object, and VS Code's reader accepts both (`isString(p.location) ? joinPath(profilesHome,
    p.location) : URI.revive(...)`), so this does too, but only when the URI's path ends in
    `User/profiles/<id>` — a URI that points anywhere else is not a profile directory of this layout.
    """
    if isinstance(location, str):
        return location if PROFILE_ID.fullmatch(location) else None
    if isinstance(location, Mapping) and isinstance(location.get("path"), str):
        *_, user, profiles, directory = ["", "", *location["path"].split("/")]
        if (user, profiles) == _URI_PARENT and PROFILE_ID.fullmatch(directory):
            return directory
    return None


def _entry_of(raw: object) -> tuple[ProfileEntry | None, str | None]:
    """One `userDataProfiles` item as an entry, or the reason it was skipped (never both).

    The reason names the profile at most; it never echoes the item, which may carry a path or an icon.
    """
    if not isinstance(raw, Mapping):
        return None, "skipped a profile entry that is not an object"
    name, flags = raw.get("name"), raw.get("useDefaultFlags", {})
    if not isinstance(name, str) or name == "":
        return None, "skipped a profile entry without a name"
    try:
        encode_profile_name(name)
    except ConfigError:
        return None, "skipped a profile whose name is not valid text"
    location = raw.get("location")
    if isinstance(location, str) and location.startswith("builtin/"):
        # Built-in profiles (Agents) ship with the app and are not the user's to sync.
        return None, f"skipped built-in profile {name!r}"
    directory = _directory_of(location)
    if directory is None:
        return None, f"skipped profile {name!r}: its location is not a single profile id"
    if not isinstance(flags, Mapping) or not all(isinstance(value, bool) for value in flags.values()):
        # Not knowing what a profile borrows would mean collecting a file VS Code never reads.
        return None, f"skipped profile {name!r}: useDefaultFlags is not a map of booleans"
    return ProfileEntry.create(
        name=name, directory=directory, borrowed=[key for key, value in flags.items() if value]
    ), None


def read_profiles(document: Any) -> ProfileMap:
    """The profiles a parsed `storage.json` lists, and what was skipped.

    Only the `userDataProfiles` key is read; the file also holds window state and a telemetry machine id,
    which are never looked at. A missing key means the user has no non-default profile (VS Code omits the
    key). A document that is not an object, or a key that is not a list, is not a profile list at all and
    comes back unreadable, so the caller keeps its records instead of reading "no profiles" as a deletion.

    Two entries that fold to one name, or that share a directory, are **both** dropped and reported: which
    one a record belongs to would otherwise depend on their order.
    """
    if not isinstance(document, Mapping):
        return ProfileMap.unreadable("storage.json is not a JSON object")
    listed = document.get(_PROFILES_KEY, [])
    if not isinstance(listed, list):
        return ProfileMap.unreadable(f"{_PROFILES_KEY} is not a list")
    parsed = [_entry_of(item) for item in listed]
    problems = [reason for _, reason in parsed if reason is not None]
    entries = [entry for entry, _ in parsed if entry is not None]
    clashing_names = set(duplicate_profile_names(entry.name for entry in entries))
    directories = [entry.directory for entry in entries]
    clashing_directories = {directory for directory in directories if directories.count(directory) > 1}
    kept = [
        e for e in entries if profile_name_key(e.name) not in clashing_names and e.directory not in clashing_directories
    ]
    problems += [
        f"skipped profile {e.name!r}: another profile has the same name or directory" for e in entries if e not in kept
    ]
    return ProfileMap.create(tuple(kept), tuple(problems))


__all__ = [
    "EXCLUDED",
    "KEYBINDINGS",
    "NOISE",
    "PROFILES",
    "PROFILE_HOME",
    "PROFILE_INDEX",
    "PROFILE_ROOT",
    "SETTINGS",
    "SNIPPETS_GLOBAL",
    "SNIPPETS_JSON",
    "VSCODE",
    "read_profiles",
]
