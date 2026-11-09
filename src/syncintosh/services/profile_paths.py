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

A versioned **config directory** (`core/config_dirs.py`, JetBrains' `DataGrip2026.2`) is the second kind of logical path
and goes through the same `resolve`: the release folder comes from the installed app (`AppConfigDir`), is read once per
cycle, and a path whose folder is unknown resolves to `Unresolved` — never to the logical text, never to the highest
folder on disk (issue #54).

The catalog decides what is a logical path (`PresetCatalog.preset_for` → `Preset.profiles`), so a path is
never treated as real merely because no `profiles` entry is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from syncintosh.core.config import LocalGlob, LocalItem, SyncConfig
from syncintosh.core.config_dirs import ConfigDirMap, ConfigDirSpec
from syncintosh.core.ports import AppConfigDir, PresetCatalog, ProfileIndex
from syncintosh.core.presets import PresetGlob
from syncintosh.core.profiles import ProfileMap, ProfilePath, ProfileSpec
from syncintosh.core.watch import ProfileTargets, WatchFilter
from syncintosh.services.doctor import Check

_NOT_TRACKED = "this machine does not track profiles for this app"
_NO_PRESET = ""
_NOT_A_PATH = "this is not a path the app's profile layout allows"
_UNREADABLE = "the app's profile list could not be read, so profile files are left as they are"
_CONFIG_NOT_TRACKED = "this machine does not track this app's settings folder"
_CONFIG_BAD_NAME = "the app named a settings folder this layout does not allow"
_OWN_SYNC_ON = "the app's own settings sync is on, so this tool leaves its settings folder alone"


def profile_directory(spec: ProfileSpec, directory: str) -> str:
    """The real `$HOME`-relative directory of a profile — the one place that joins `home` and an id."""
    return f"{spec.home}/{directory}"


@dataclass(frozen=True)
class Located:
    """A path resolved to a real `$HOME`-relative one.

    `profile_directory` is set for a profile file: the profile's own directory, which must already
    exist for a write (this module never creates one). `config_directory` is the same for a file in an app's
    versioned config directory: the release folder, which an IDE's first run populates and which this code never
    creates. Both are `None` for an ordinary path.
    """

    path: str
    profile_directory: str | None = None
    config_directory: str | None = None

    @property
    def owner_directory(self) -> str | None:
        """The directory that must already exist for a write, whichever kind of logical path this was."""
        return self.profile_directory or self.config_directory


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
class ConfigDirView:
    """One versioned config layout and the folder this machine's app uses for it, read once for the cycle."""

    spec: ConfigDirSpec
    preset: str
    directory: ConfigDirMap


@dataclass(frozen=True)
class LivePaths:
    """The resolver for one cycle. Build it with `open_live_paths`; the empty value resolves nothing logical."""

    catalog: PresetCatalog
    views: tuple[ProfileView, ...] = ()
    config_views: tuple[ConfigDirView, ...] = ()

    def config_dir(self, path: str) -> tuple[ConfigDirSpec, str] | None:
        """The layout and relative file when `path` is a logical config-directory path of a preset the catalog has."""
        preset = self.catalog.preset_for(path)
        spec = None if preset is None else preset.config_dir
        parsed = None if spec is None else spec.parse(path)
        return None if spec is None or parsed is None else (spec, parsed.relative)

    def _config_directory(self, spec: ConfigDirSpec) -> str | Unresolved:
        """The real release folder for `spec` on this machine, or why there is none."""
        view = next((candidate for candidate in self.config_views if candidate.spec == spec), None)
        if view is None:
            return Unresolved(_CONFIG_NOT_TRACKED)
        if view.directory.directory is None:
            return Unresolved(view.directory.reason or _CONFIG_NOT_TRACKED)
        if not spec.is_release_name(view.directory.directory):
            # The port promises a validated name, but this is the line a write is built from.
            return Unresolved(_CONFIG_BAD_NAME)
        if view.directory.settings_sync_enabled:
            # Two writers on one file: the app's own sync and this one. Collect and apply both resolve through here,
            # so neither touches the folder; the records are kept (an unresolved path is never a deletion).
            return Unresolved(spec.sync_conflict or _OWN_SYNC_ON)
        return spec.real_directory(view.directory.directory)

    def resolve_directory(self, preset: str, directory: str) -> str | Unresolved:
        """The real directory a (possibly logical) glob directory of `preset` stands for, or why there is none.

        Only a logical config directory is rewritten; every other directory is returned as it is. A glob listing
        goes through this, so a versioned directory is listed where the app really keeps it. `preset` is the name
        this machine's own config gives the glob, resolved through the local catalog.
        """
        owner = self.catalog.get(preset)
        spec = None if owner is None else owner.config_dir
        relative = None if spec is None else spec.relative_directory(directory)
        if spec is None or relative is None:
            return directory
        real = self._config_directory(spec)
        if isinstance(real, Unresolved):
            return real
        return real if relative == "" else f"{real}/{relative}"

    def logical(self, path: str) -> tuple[ProfileSpec, ProfilePath] | None:
        """The layout and parts when `path` is a logical profile path of a preset the catalog knows."""
        preset = self.catalog.preset_for(path)
        spec = None if preset is None else preset.profiles
        parsed = None if spec is None else spec.parse(path)
        return None if spec is None or parsed is None else (spec, parsed)

    def resolve(self, path: str) -> Located | Unresolved:
        """The real path for `path`, or why a logical one has none here."""
        config = self.config_dir(path)
        if config is not None:
            spec, relative = config
            real = self._config_directory(spec)
            if isinstance(real, Unresolved):
                return real
            return Located(f"{real}/{relative}", config_directory=real)
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
        """How a person should see `path`: `vscode profile "Work": settings.json` for a logical one.

        A config-directory path reads `datagrip: options/editor-font.xml (DataGrip2026.2)`: the release folder when
        this resolver knows it, and no path or value either way.
        """
        preset = self.catalog.preset_for(path)
        config = self.config_dir(path)
        if config is not None and preset is not None:
            view = next((candidate for candidate in self.config_views if candidate.spec == config[0]), None)
            release = None if view is None else view.directory.directory
            return f"{preset.name}: {config[1]}" + ("" if release is None else f" ({release})")
        found = self.logical(path)
        if found is None or preset is None:
            return path
        return f'{preset.name} profile "{found[1].name}": {found[1].relative}'


def open_live_paths(
    config: SyncConfig,
    catalog: PresetCatalog,
    index: ProfileIndex | None,
    config_dirs: AppConfigDir | None = None,
) -> LivePaths:
    """Read each configured layout's profile list and config directory once and bind the resolver for this cycle.

    This is the I/O step (the index read, the app's metadata read); constructing the resolver is not. Without a
    port the views are empty, so a logical record resolves to nothing rather than to a guess.
    """
    profile_views: tuple[ProfileView, ...] = ()
    if index is not None:
        specs = {entry.spec.index: entry.spec for entry in config.profiles}
        profile_views = tuple(ProfileView(spec=spec, profiles=index.read(spec)) for spec in specs.values())
    return LivePaths(catalog=catalog, views=profile_views, config_views=_read_config_dirs(config, catalog, config_dirs))


def _read_config_dirs(
    config: SyncConfig, catalog: PresetCatalog, config_dirs: AppConfigDir | None
) -> tuple[ConfigDirView, ...]:
    """One view per config layout a tracked preset uses, each read once (preset names come from the local config)."""
    if config_dirs is None:
        return ()
    names = dict.fromkeys(entry.preset for entry in (*config.items, *config.globs))
    owners = [owner for name in names if (owner := catalog.get(name)) is not None and owner.config_dir is not None]
    return tuple(
        ConfigDirView(spec=owner.config_dir, preset=owner.name, directory=config_dirs.read(owner.config_dir))
        for owner in owners
        if owner.config_dir is not None
    )


def config_dir_targets(config: SyncConfig, paths: LivePaths) -> SyncConfig:
    """`config` with every logical config-directory item and glob replaced by the real one this machine has.

    A watch observes real paths, and a record's logical text never names a file the OS reports an event on. An item
    whose release folder is unknown is left out (nothing to watch yet; the polling sync picks the app up once it
    is installed).
    """
    items: list[LocalItem] = []
    for item in config.items:
        located = paths.resolve(item.path) if paths.config_dir(item.path) is not None else Located(item.path)
        if isinstance(located, Located):
            items.append(replace(item, path=located.path))
    globs: list[LocalGlob] = []
    for entry in config.globs:
        directory = paths.resolve_directory(entry.preset, entry.glob.directory)
        if isinstance(directory, Unresolved):
            continue
        globs.append(replace(entry, glob=PresetGlob.create(f"{directory}/{entry.glob.name}")))
    return replace(config, items=tuple(items), globs=tuple(globs))


def config_dir_summary(config: SyncConfig, paths: LivePaths) -> Check | None:
    """The doctor row about versioned config directories, or `None` when no tracked preset uses one.

    Informational, like the profiles row. The sentence names the release folder and notes (several installs), never a
    path outside the home layout or any content.
    """
    if not paths.config_views:
        return None
    parts: list[str] = []
    for view in paths.config_views:
        found = view.directory
        parts.append(
            f"{view.preset}: {found.directory}"
            if found.directory is not None
            else f"{view.preset}: {found.reason}; its records are kept as they are"
        )
        if found.directory is not None and found.settings_sync_enabled:
            parts.append(f"{view.preset}: {view.spec.sync_conflict or _OWN_SYNC_ON}")
        parts.extend(f"{view.preset}: {problem}" for problem in found.problems)
    # A sync that is held back is a problem to act on (turn one of the two off), not a note.
    held = any(
        view.directory.directory is not None and view.directory.settings_sync_enabled for view in paths.config_views
    )
    return Check("config folders", not held, "; ".join(parts))


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


def watch_targets(
    config: SyncConfig, catalog: PresetCatalog, index: ProfileIndex | None, config_dirs: AppConfigDir | None = None
) -> WatchFilter:
    """The filter a watch uses right now: the config's paths plus this machine's profile files and list.

    Called at start and again after each sync, so a profile created in the app, or a release the app moved to,
    is picked up. A logical config-directory path is watched where it really is.
    """
    paths = open_live_paths(config, catalog, index, config_dirs)
    return WatchFilter.create(config_dir_targets(config, paths), profile_targets(config, paths))


def describe_with(catalog: PresetCatalog, path: str) -> str:
    """`LivePaths.describe` for a caller with only a catalog (a renderer): the label, no index read."""
    return LivePaths(catalog=catalog).describe(path)


__all__ = [
    "ConfigDirView",
    "LivePaths",
    "Located",
    "ProfileView",
    "Unresolved",
    "config_dir_summary",
    "config_dir_targets",
    "describe_with",
    "open_live_paths",
    "profile_summary",
    "profile_targets",
    "watch_targets",
]
