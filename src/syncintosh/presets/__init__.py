# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in preset catalog: the data `PresetCatalog` reads.

One module per preset holds the paths, the strip lists, the validator, and the download
metadata as **validated frozen data**, with the Step 0 evidence in its docstring
(``AGENTS.md`` §3.6, the `add-preset` skill). This module only assembles them and answers the
three catalog questions; no behaviour beyond lookup lives here (``AGENTS.md`` §2.2).

The catalog is the **local** authority. A record's `preset` arrives from the repo, so
`policy_for` matches on path and `knows` only checks that a built-in preset name did not arrive
with a path that preset does not own (``docs/design/03-manifest.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

from syncintosh.core.errors import ConfigError
from syncintosh.core.paths import collision_key, duplicate_paths
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.presets.zsh import ZSH

# Every built-in preset, in the order a catalog lists them.
BUILTIN_PRESETS: tuple[Preset, ...] = (ZSH,)


@dataclass(frozen=True)
class BuiltinCatalog:
    """The shipped presets, indexed for path lookup.

    Built through :meth:`create`, which refuses two presets claiming one path: the second would
    shadow the first's strip policy and make which keys travel depend on catalog order.
    """

    presets: tuple[Preset, ...]

    @classmethod
    def create(cls, presets: tuple[Preset, ...]) -> BuiltinCatalog:
        """Validate cross-preset path uniqueness and freeze.

        A duplicate *within* one preset is already refused by `Preset.create`; what is left to
        catch here is the same path claimed by two presets.
        """
        clashes = duplicate_paths(file.path for preset in presets for file in preset.files)
        if clashes:
            breeders = sorted(
                {
                    preset.name
                    for preset in presets
                    for file in preset.files
                    if any(collision_key(file.path) == clash for clash in clashes)
                }
            )
            raise ConfigError(f"two built-in presets claim one path: {list(clashes)} ({', '.join(breeders)})")
        return cls(presets=presets)

    @classmethod
    def default(cls) -> BuiltinCatalog:
        """The catalog this build ships."""
        return cls.create(BUILTIN_PRESETS)

    def get(self, name: str) -> Preset | None:
        """The built-in preset called `name`, or `None` when there is no such preset."""
        return next((preset for preset in self.presets if preset.name == name), None)

    def policy_for(self, path: str) -> PresetFile | None:
        """The policy for `path`, matched on the folded path, or `None` when unowned.

        Matched on path — never on the preset name a record carries — so a synced record cannot
        choose a weaker policy by naming a different preset (``docs/design/03-manifest.md``).
        """
        folded = collision_key(path)
        return next(
            (file for preset in self.presets for file in preset.files if collision_key(file.path) == folded),
            None,
        )

    def knows(self, preset: str, path: str) -> bool:
        """True when `path` is one `preset` may name here.

        A name that is not built-in makes no claim to check, so it passes and the local
        allowlist alone decides; a built-in name must own the path, so a record that arrived
        claiming `zsh` cannot be applied to an arbitrary file.
        """
        builtin = self.get(preset)
        if builtin is None:
            return True
        folded = collision_key(path)
        return any(collision_key(file.path) == folded for file in builtin.files)


__all__ = ["BUILTIN_PRESETS", "BuiltinCatalog"]
