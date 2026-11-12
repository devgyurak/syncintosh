# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: the configuration a fresh `init` writes, so the policy is not in `cli/`.

The tracked items, their scope and format, and the machine identity are domain choices, not
argument parsing; keeping them here lets a second front-end reuse them (``AGENTS.md`` §2.3).

The items are **expanded from the local preset catalog**, never named path by path: the catalog
is the one place that knows which files a preset owns and in which format, so `init` cannot
write a configuration the catalog would later refuse to apply (``docs/design/03-manifest.md``).
`services` reaches the catalog through the port, so it never imports `presets/`
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final, Literal

from syncintosh.core.config import LocalGlob, LocalItem, LocalProfiles, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.core.paths import collision_key, duplicate_paths
from syncintosh.core.ports import FileIdSource, PresetCatalog
from syncintosh.core.profiles import profile_name_key

# What `add_preset` did to the preset's `profiles` entry. Plain strings, not an Enum: they are shown to the person
# verbatim and a mixed `str` enum can stringify to `Kind.NAME` (``AGENTS.md`` §4).
PROFILES_NONE: Final = "none"  # the preset has no profiles layout, so there is nothing to say
PROFILES_ADDED: Final = "added"  # a new entry was written
PROFILES_MERGED: Final = "merged"  # new names were added to the existing list
PROFILES_KEPT: Final = "kept"  # the existing list stays as it is, since no --profile widened it
PROFILES_UNCHANGED: Final = "unchanged"  # the entry already covered what was asked for
ProfilesOutcome = Literal["none", "added", "merged", "kept", "unchanged"]


def mint_machine_id(source: FileIdSource) -> str:
    """A fresh, random machine identity.

    Minted through the `FileIdSource` port so `services` never reaches for OS entropy itself
    (``AGENTS.md`` §2.1).
    """
    return source.new_file_id()


@dataclass(frozen=True)
class _Expansion:
    """What one preset contributes to a config: the entries `init` and `preset add` both build from it."""

    preset: str
    items: tuple[LocalItem, ...]
    globs: tuple[LocalGlob, ...]
    profiles: LocalProfiles | None

    @classmethod
    def create(
        cls, *, preset: str, items: Iterable[LocalItem], globs: Iterable[LocalGlob], profiles: LocalProfiles | None
    ) -> _Expansion:
        return cls(preset=preset, items=tuple(items), globs=tuple(globs), profiles=profiles)


def _expand_preset(name: str, catalog: PresetCatalog, profile_names: tuple[str, ...] | None) -> _Expansion:
    """One preset's entries, common scope. The one place that decides what a preset name expands to.

    An unknown preset name is refused rather than skipped — silently tracking nothing looks exactly like a
    successful setup, and the user would only find out at the first sync.

    A preset file whose last element is a glob (`snippets/*.json`) becomes a config **glob**, not an item: which
    files it names is a per-machine fact, so the config records the directory pattern and the collect step
    expands it here (`core/config.LocalGlob`).

    `profile_names` is `None` for every profile this machine has (`"*"`), or the names to allow.
    """
    preset = catalog.get(name)
    if preset is None:
        raise ConfigError(f"unknown preset {name!r}")
    if preset.agent_projects is not None:
        # Its files are listed for a project's element, which only this machine's own mapping can fill in; a glob
        # of the template would track a directory called `@` and sync nothing, which looks like a working setup.
        raise ConfigError(
            f"{name!r} syncs per-project data: map the project under `agent_projects` in config.json "
            "and list the preset there (docs/design/presets.md)"
        )
    if profile_names is not None and preset.profiles is None:
        raise ConfigError(f"{name!r} has no profiles, so --profile does not apply")
    profiles = None
    if preset.profiles is not None:
        # Which profiles exist is read at sync time from the app's own list; naming "*" or a name here allows
        # it, it does not create any (decision D3, D5).
        profiles = LocalProfiles.create(
            preset=preset.name,
            spec=preset.profiles,
            scope="common",
            names="*" if profile_names is None else profile_names,
        )
    items = tuple(
        LocalItem.create(path=file.path, preset=preset.name, scope="common", format=file.format)
        for file in preset.files
        if not file.is_glob
    )
    globs = tuple(
        LocalGlob.create(path=file.path, preset=preset.name, scope="common", format=file.format)
        for file in preset.files
        if file.is_glob
    )
    return _Expansion.create(preset=preset.name, items=items, globs=globs, profiles=profiles)


def _refuse_duplicates(items: Iterable[LocalItem], globs: Iterable[LocalGlob]) -> None:
    duplicated = duplicate_paths([item.path for item in items] + [entry.glob.path for entry in globs])
    if duplicated:
        # Two entries for one file would carry two records and write it twice, which the manifest's
        # path-collision rule then reports as a conflict on every sync.
        raise ConfigError(f"these paths would be tracked twice: {list(duplicated)}")


def new_machine_config(
    *,
    remote: str,
    machine_id: str,
    preset_names: Iterable[str],
    catalog: PresetCatalog,
) -> SyncConfig:
    """The configuration for a first `init`: every file of every named preset, common scope.

    A name given twice is deduplicated rather than refused: `--preset zsh --preset zsh` is a repetition, not a
    second file, and the paths it expands to are the same either way.
    """
    expansions = [_expand_preset(name, catalog, None) for name in dict.fromkeys(preset_names)]
    items = tuple(item for each in expansions for item in each.items)
    globs = tuple(entry for each in expansions for entry in each.globs)
    _refuse_duplicates(items, globs)
    return SyncConfig.create(
        remote=remote,
        machine_id=machine_id,
        items=items,
        globs=globs,
        profiles=tuple(each.profiles for each in expansions if each.profiles is not None),
    )


@dataclass(frozen=True)
class PresetAddition:
    """What `add_preset` produced: the new config and what changed, for the command to report.

    `added` and `skipped` are the tracked paths (a glob as its pattern). `profiles` says what happened to the
    preset's `profiles` entry, and `profile_names` are the names that entry gained (`added` and `merged` only).
    """

    config: SyncConfig
    added: tuple[str, ...]
    skipped: tuple[str, ...]
    profiles: ProfilesOutcome
    profile_names: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """True when `config` differs from the one given, so there is something to save."""
        return bool(self.added) or self.profiles in (PROFILES_ADDED, PROFILES_MERGED)

    @classmethod
    def create(
        cls,
        *,
        config: SyncConfig,
        added: Iterable[str],
        skipped: Iterable[str],
        profiles: ProfilesOutcome,
        profile_names: Iterable[str] = (),
    ) -> PresetAddition:
        return cls(
            config=config,
            added=tuple(added),
            skipped=tuple(skipped),
            profiles=profiles,
            profile_names=tuple(profile_names),
        )


def _fold_entries[Entry: (LocalItem, LocalGlob)](
    existing: tuple[Entry, ...], wanted: tuple[Entry, ...], path_of: Callable[[Entry], str]
) -> tuple[tuple[Entry, ...], tuple[str, ...], tuple[str, ...]]:
    """`existing` plus what `wanted` adds, and the paths added and skipped.

    A path already tracked by the same preset and format is skipped and its scope and machine stay as the person
    set them. The same path under another preset or format is refused: one of the two would silently stop being
    synced, and which one is the person's call.
    """
    by_key = {collision_key(path_of(entry)): entry for entry in existing}
    kept: list[Entry] = []
    added: list[str] = []
    skipped: list[str] = []
    for entry in wanted:
        current = by_key.get(collision_key(path_of(entry)))
        if current is None:
            kept.append(entry)
            added.append(path_of(entry))
        elif (current.preset, current.format) == (entry.preset, entry.format):
            skipped.append(path_of(entry))
        else:
            raise ConfigError(
                f"{path_of(entry)!r} is already tracked as {current.preset!r} ({current.format}), "
                f"not {entry.preset!r} ({entry.format}); remove that entry from config.json first"
            )
    return (*existing, *kept), tuple(added), tuple(skipped)


def _merge_profiles(
    existing: LocalProfiles | None, wanted: LocalProfiles | None, asked: tuple[str, ...] | None
) -> tuple[LocalProfiles | None, ProfilesOutcome, tuple[str, ...]]:
    """The preset's `profiles` entry after the add, what was done, and the names gained.

    The entry only ever narrows to what the person named or stays as it is: a list is never widened to `"*"`,
    which would start syncing profiles the person left out, and an existing entry keeps its scope and machine.
    """
    if wanted is None:
        return None, PROFILES_NONE, ()
    if existing is None:
        return wanted, PROFILES_ADDED, () if wanted.names == "*" else wanted.names
    if existing.names == "*":
        return existing, PROFILES_UNCHANGED, ()
    if asked is None:
        return existing, PROFILES_KEPT, ()
    known = {profile_name_key(each) for each in existing.names}
    gained = tuple(name for name in asked if profile_name_key(name) not in known)
    if not gained:
        return existing, PROFILES_UNCHANGED, ()
    merged = LocalProfiles.create(
        preset=existing.preset,
        spec=existing.spec,
        scope=existing.scope,
        machine=existing.machine,
        names=(*existing.names, *gained),
    )
    return merged, PROFILES_MERGED, gained


def _distinct_names(names: Iterable[str]) -> tuple[str, ...]:
    """`names` with a repeat under `profile_name_key` dropped; the first spelling wins."""
    first: dict[str, str] = {}
    for name in names:
        first.setdefault(profile_name_key(name), name)
    return tuple(first.values())


def add_preset(
    config: SyncConfig,
    *,
    name: str,
    catalog: PresetCatalog,
    profile_names: tuple[str, ...] | None = None,
) -> PresetAddition:
    """`config` with one built-in preset added by the rules `init` uses, and what changed.

    What is already tracked is skipped (see `_fold_entries`), so running it twice changes nothing. New entries are
    appended after the existing ones and everything else in `config` is carried over unchanged. `profile_names`
    limits the preset's profiles to those names; it is refused for a preset that has none. A name given twice
    (under `collision_key`) is one name.
    """
    asked = None if profile_names is None else _distinct_names(profile_names)
    if asked == ():
        raise ConfigError("--profile needs a profile name")
    expansion = _expand_preset(name, catalog, asked)
    items, added_items, skipped_items = _fold_entries(config.items, expansion.items, lambda item: item.path)
    globs, added_globs, skipped_globs = _fold_entries(config.globs, expansion.globs, lambda entry: entry.glob.path)
    _refuse_duplicates(items, globs)
    current = next((entry for entry in config.profiles if entry.preset == expansion.preset), None)
    entry, outcome, gained = _merge_profiles(current, expansion.profiles, asked)
    if current is None:
        profiles = (*config.profiles, *(() if entry is None else (entry,)))
    else:
        profiles = tuple(entry if each is current and entry is not None else each for each in config.profiles)
    return PresetAddition.create(
        config=SyncConfig.create(
            remote=config.remote,
            machine_id=config.machine_id,
            items=items,
            globs=globs,
            llm=config.llm,
            profiles=profiles,
            agent_projects=config.agent_projects,
        ),
        added=(*added_items, *added_globs),
        skipped=(*skipped_items, *skipped_globs),
        profiles=outcome,
        profile_names=gained,
    )


__all__ = [
    "PROFILES_ADDED",
    "PROFILES_KEPT",
    "PROFILES_MERGED",
    "PROFILES_NONE",
    "PROFILES_UNCHANGED",
    "PresetAddition",
    "ProfilesOutcome",
    "add_preset",
    "mint_machine_id",
    "new_machine_config",
]
