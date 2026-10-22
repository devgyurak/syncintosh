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

from collections.abc import Iterable

from syncintosh.core.config import LocalGlob, LocalItem, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.core.paths import duplicate_paths
from syncintosh.core.ports import FileIdSource, PresetCatalog


def mint_machine_id(source: FileIdSource) -> str:
    """A fresh, random machine identity.

    Minted through the `FileIdSource` port so `services` never reaches for OS entropy itself
    (``AGENTS.md`` §2.1).
    """
    return source.new_file_id()


def new_machine_config(
    *,
    remote: str,
    machine_id: str,
    preset_names: Iterable[str],
    catalog: PresetCatalog,
) -> SyncConfig:
    """The configuration for a first `init`: every file of every named preset, common scope.

    An unknown preset name is refused rather than skipped — silently tracking nothing looks
    exactly like a successful setup, and the user would only find out at the first sync. A name
    given twice is deduplicated rather than refused: `--preset zsh --preset zsh` is a
    repetition, not a second file, and the paths it expands to are the same either way.

    A preset file whose last element is a glob (`snippets/*.json`) becomes a config **glob**, not an
    item: which files it names is a per-machine fact, so the config records the directory pattern and
    the collect step expands it here (`core/config.LocalGlob`).
    """
    items: list[LocalItem] = []
    globs: list[LocalGlob] = []
    for name in dict.fromkeys(preset_names):
        preset = catalog.get(name)
        if preset is None:
            raise ConfigError(f"unknown preset {name!r}")
        for file in preset.files:
            if file.is_glob:
                globs.append(LocalGlob.create(path=file.path, preset=preset.name, scope="common", format=file.format))
            else:
                items.append(LocalItem.create(path=file.path, preset=preset.name, scope="common", format=file.format))
    duplicated = duplicate_paths([item.path for item in items] + [entry.glob.path for entry in globs])
    if duplicated:
        # Two entries for one file would carry two records and write it twice, which the manifest's
        # path-collision rule then reports as a conflict on every sync.
        raise ConfigError(f"these paths would be tracked twice: {list(duplicated)}")
    return SyncConfig.create(remote=remote, machine_id=machine_id, items=tuple(items), globs=tuple(globs))


__all__ = ["mint_machine_id", "new_machine_config"]
