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
from syncintosh.core.presets import Preset, PresetFile, ShippedPresets
from syncintosh.presets.docker import DOCKER
from syncintosh.presets.vscode import VSCODE
from syncintosh.presets.zsh import ZSH

# Every built-in preset, in the order a catalog lists them.
BUILTIN_PRESETS: tuple[Preset, ...] = (ZSH, VSCODE, DOCKER)


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
        cls._refuse_glob_over_exact(presets)
        return cls(presets=presets)

    @staticmethod
    def _refuse_glob_over_exact(presets: tuple[Preset, ...]) -> None:
        """Refuse a glob that covers an exact path any preset already owns.

        A glob owns a directory, and an exact path must not fall inside it — not another preset's,
        and not the same preset's: two entries would claim one file, and which policy applied (for
        one, which credential keys are stripped) would depend on catalog order. Detected here rather
        than at collect, so a catalog cannot be built that is ambiguous.
        """
        exact = [(preset.name, file.path) for preset in presets for file in preset.files if not file.is_glob]
        covered = sorted(
            f"{preset.name} glob {file.path} covers {other}:{path}"
            for preset in presets
            for file in preset.files
            if file.glob is not None
            for other, path in exact
            if file.glob.matches(path)
        )
        if covered:
            raise ConfigError(f"a preset glob covers another preset's path: {covered}")

    @classmethod
    def default(cls) -> BuiltinCatalog:
        """The catalog this build ships."""
        return cls.create(BUILTIN_PRESETS)

    def shipped(self) -> ShippedPresets:
        """This catalog's presets as the set download URLs are trusted against.

        Only the shipped catalog (`default()`) should reach a caller through here. A catalog built
        from anything else would hand its URLs trust it did not earn.
        """
        return ShippedPresets.create(self.presets)

    def get(self, name: str) -> Preset | None:
        """The built-in preset called `name`, or `None` when there is no such preset."""
        return next((preset for preset in self.presets if preset.name == name), None)

    def policy_for(self, path: str) -> PresetFile | None:
        """The policy for `path`, matched on the folded path, or `None` when unowned.

        Matched on path — never on the preset name a record carries — so a synced record cannot
        choose a weaker policy by naming a different preset (``docs/design/03-manifest.md``). A
        glob entry matches by its directory and last element. A profile's logical path takes the
        policy of its default-profile counterpart; this is the one place that mapping lives, so a
        later rule that refines a profile's policy (issue #23) has one lookup to extend.
        """
        return next(
            (file for preset in self.presets for file in preset.files if file.matches(self._policy_path(preset, path))),
            None,
        )

    @staticmethod
    def _policy_path(preset: Preset, path: str) -> str:
        """The path whose policy `path` takes: its default-profile counterpart for a profile path, else itself."""
        parsed = preset.profiles.parse(path) if preset.profiles is not None else None
        return path if parsed is None else parsed.default_path

    @staticmethod
    def _owns(preset: Preset, path: str) -> bool:
        """True when `preset` owns `path`, directly or as a logical profile path of its own layout."""
        return any(file.matches(path) for file in preset.files) or (
            preset.profiles is not None and preset.profiles.parse(path) is not None
        )

    def knows(self, preset: str, path: str) -> bool:
        """True when `path` is one `preset` may name here.

        A name that is not built-in makes no claim to check, so it passes and the local
        allowlist alone decides; a built-in name must own the path, so a record that arrived
        claiming `zsh` cannot be applied to an arbitrary file.
        """
        if any(self._reserved(each, path) for each in self.presets):
            # Under a profiles home only a logical path is a path at all. A raw id there would be collected
            # with no policy, whatever the record's preset name claims (a non-built-in name makes no claim).
            return False
        builtin = self.get(preset)
        if builtin is None:
            return True
        return self._owns(builtin, path)

    @staticmethod
    def _reserved(preset: Preset, path: str) -> bool:
        """True when `path` is under `preset`'s profiles home but is not a logical profile path."""
        spec = preset.profiles
        if spec is None:
            return False
        parts, home = (
            [collision_key(part) for part in path.split("/")],
            [collision_key(p) for p in spec.home.split("/")],
        )
        return parts[: len(home)] == home and len(parts) > len(home) and spec.parse(path) is None

    def preset_for(self, path: str) -> Preset | None:
        """The built-in preset that owns `path`, or `None` when none does.

        Matched on the folded path, the same way `policy_for` is — including a **glob** entry, so a
        file expanded from `snippets/*.json` still reports the preset, its download page, and its
        cask — and never on the `preset` name a record arrived with (``AGENTS.md`` §3.6).
        """
        return next((preset for preset in self.presets if self._owns(preset, path)), None)


__all__ = ["BUILTIN_PRESETS", "BuiltinCatalog"]
