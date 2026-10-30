# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a record's path lives on this machine: the one resolver every live read and write goes through.

Most records name a real `$HOME`-relative path. A profile record names a **logical** one
(`…/profiles/@Work/settings.json`, `core/profiles.py`), because the profile's real directory is a random
id that differs per Mac. This module turns either into the real path, or says why it cannot:

* `resolve` is the single place the collect read, the apply write, the ledger settle and `diff` build a
  live path. A logical path that does not resolve returns `Unresolved`, and the caller ends in a
  deferral — it never falls back to the logical text, because that would read or create a directory that
  is not a profile (issue #22, decisions D5–D7).
* `open_live_paths` reads each profile index **once per cycle**, through the `ProfileIndex` port. An
  index that cannot be read resolves no profile and keeps every record (the unresolved reason says so).
* Nothing here creates a profile directory or touches the index. A profile this machine lacks is a
  non-blocking deferral, and the person creates it in the app.

The catalog decides what is a logical path (`PresetCatalog.preset_for` → `Preset.profiles`), so a path is
never treated as real merely because no `profiles` entry is configured.
"""

from __future__ import annotations

from dataclasses import dataclass

from syncintosh.core.config import SyncConfig
from syncintosh.core.ports import PresetCatalog, ProfileIndex
from syncintosh.core.presets import PresetGlob
from syncintosh.core.profiles import ProfileMap, ProfilePath, ProfileSpec
from syncintosh.core.watch import ProfileTargets, WatchFilter
from syncintosh.services.doctor import Check

_NOT_TRACKED = "this machine does not track profiles for this app"
_NO_PRESET = ""
_NOT_A_PATH = "this is not a path the app's profile layout allows"
_UNREADABLE = "the app's profile list could not be read, so profile files are left as they are"


def profile_directory(spec: ProfileSpec, directory: str) -> str:
    """The real `$HOME`-relative directory of a profile — the one place that joins `home` and an id."""
    return f"{spec.home}/{directory}"


@dataclass(frozen=True)
class Located:
    """A path resolved to a real `$HOME`-relative one.

    `profile_directory` is set for a profile file: the profile's own directory, which must already
    exist for a write (this module never creates one). `None` for an ordinary path.
    """

    path: str
    profile_directory: str | None = None


@dataclass(frozen=True)
class Unresolved:
    """A logical path with no real one here, and the sentence that says what to do."""

    reason: str


@dataclass(frozen=True)
class ProfileView:
    """One layout and the profiles this machine has under it, read once for the cycle."""

    spec: ProfileSpec
    profiles: ProfileMap


@dataclass(frozen=True)
class LivePaths:
    """The resolver for one cycle. Build it with `open_live_paths`; the empty value resolves nothing logical."""

    catalog: PresetCatalog
    views: tuple[ProfileView, ...] = ()

    def logical(self, path: str) -> tuple[ProfileSpec, ProfilePath] | None:
        """The layout and parts when `path` is a logical profile path of a preset the catalog knows."""
        preset = self.catalog.preset_for(path)
        spec = None if preset is None else preset.profiles
        parsed = None if spec is None else spec.parse(path)
        return None if spec is None or parsed is None else (spec, parsed)

    def resolve(self, path: str) -> Located | Unresolved:
        """The real path for `path`, or why a logical one has none here."""
        found = self.logical(path)
        if found is None:
            if not self.catalog.knows(_NO_PRESET, path):
                # Under a profiles home but not a well-formed logical path (a raw id, or an element written
                # from a decomposed name). A name that is not built in claims nothing, so `knows` is False only
                # for such a reserved path. Callers gate on `knows` already; this keeps `resolve` safe alone.
                return Unresolved(_NOT_A_PATH)
            return Located(path)
        spec, parsed = found
        view = next((candidate for candidate in self.views if candidate.spec == spec), None)
        if view is None:
            return Unresolved(_NOT_TRACKED)
        if not view.profiles.readable:
            return Unresolved(_UNREADABLE)
        entry = view.profiles.lookup(parsed.name)
        if entry is None:
            return Unresolved(f'create the profile "{parsed.name}" in the app to sync it here')
        if parsed.resource in entry.borrowed:
            return Unresolved(f'the profile "{parsed.name}" uses the default profile\'s {parsed.resource}')
        directory = profile_directory(spec, entry.directory)
        return Located(f"{directory}/{parsed.relative}", profile_directory=directory)

    def describe(self, path: str) -> str:
        """How a person should see `path`: `vscode profile "Work": settings.json` for a logical one."""
        found = self.logical(path)
        preset = self.catalog.preset_for(path)
        if found is None or preset is None:
            return path
        return f'{preset.name} profile "{found[1].name}": {found[1].relative}'


def open_live_paths(config: SyncConfig, catalog: PresetCatalog, index: ProfileIndex | None) -> LivePaths:
    """Read each configured layout's profile list once and bind the resolver for this cycle.

    This is the I/O step (the index read); constructing the resolver is not. Without an index port the
    views are empty, so a profile record resolves to nothing rather than to a guess.
    """
    if index is None:
        return LivePaths(catalog=catalog)
    specs = {entry.spec.index: entry.spec for entry in config.profiles}
    return LivePaths(
        catalog=catalog,
        views=tuple(ProfileView(spec=spec, profiles=index.read(spec)) for spec in specs.values()),
    )


def profile_targets(config: SyncConfig, paths: LivePaths) -> ProfileTargets:
    """The real paths a watch should add for the profiles this machine has, and the index files to follow.

    A borrowed resource is left out (its file is the default profile's, already watched). The index is
    included even when it could not be read, so fixing it triggers a sync.
    """
    files: list[str] = []
    globs: list[PresetGlob] = []
    indexes: list[str] = []
    for entry in config.profiles:
        view = next((candidate for candidate in paths.views if candidate.spec == entry.spec), None)
        if view is None:
            continue
        indexes.append(view.spec.index)
        for profile in view.profiles.entries:
            if not entry.allows(profile.name):
                continue
            for file in view.spec.files:
                if file.resource in profile.borrowed:
                    continue
                real = f"{profile_directory(view.spec, profile.directory)}/{file.path}"
                if any(character in file.path for character in "*?"):
                    globs.append(PresetGlob.create(real))
                else:
                    files.append(real)
    return ProfileTargets(
        paths=tuple(dict.fromkeys(files)), globs=tuple(dict.fromkeys(globs)), indexes=tuple(dict.fromkeys(indexes))
    )


def profile_summary(config: SyncConfig, paths: LivePaths) -> Check | None:
    """The doctor row about profiles, or `None` when no profile layout is configured.

    Informational: a machine without the app has no profile list, and that is not a broken install, so the
    caller shows it as a note rather than a failure. The sentence names profiles and counts, never paths or
    file content.
    """
    if not config.profiles:
        return None
    parts: list[str] = []
    for view in paths.views:
        if not view.profiles.readable:
            parts.append(f"{view.profiles.problems[0]}; profile records are kept as they are")
            continue
        names = ", ".join(f'"{entry.name}"' for entry in view.profiles.entries) or "none"
        parts.append(f"profiles here: {names}")
        parts.extend(view.profiles.problems)
    return Check("profiles", True, "; ".join(parts) or "no profile list configured")


def watch_targets(config: SyncConfig, catalog: PresetCatalog, index: ProfileIndex | None) -> WatchFilter:
    """The filter a watch uses right now: the config's paths plus this machine's profile files and list.

    Called at start and again after each sync, so a profile created in the app is picked up.
    """
    return WatchFilter.create(config, profile_targets(config, open_live_paths(config, catalog, index)))


def describe_with(catalog: PresetCatalog, path: str) -> str:
    """`LivePaths.describe` for a caller with only a catalog (a renderer): the label, no index read."""
    return LivePaths(catalog=catalog).describe(path)


__all__ = [
    "LivePaths",
    "Located",
    "ProfileView",
    "Unresolved",
    "describe_with",
    "open_live_paths",
    "profile_summary",
    "profile_targets",
    "watch_targets",
]
