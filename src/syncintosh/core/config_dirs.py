# SPDX-License-Identifier: AGPL-3.0-or-later
"""An app's **versioned** config directory, addressed by a logical path: the spec, the codec, and the resolved map.

JetBrains keeps a product's settings under a folder named after the release (`DataGrip2026.2`), so two Macs on
different releases would never share a record keyed by the real path (issue #54). A record instead carries a
**logical path** that names the product and not the release:

```
Library/Application Support/JetBrains/@DataGrip/options/editor-font.xml
```

* The leading ``@`` marks the element as logical. A real release folder is ``<prefix><year>.<n>`` and never
  holds it, so a logical path cannot collide with a real one (the same device as a VS Code profile, `core/profiles.py`).
* The manifest schema is unchanged. A build that predates this owns no such path, so it refuses the record at the
  catalog check — the fail-closed direction.
* Which release folder this machine uses is **never** guessed from the disk (the highest folder may be a stale one
  of an uninstalled release). It comes from the installed app itself, through the `AppConfigDir` port, and is read
  once per cycle. Until a path is resolved, nothing may be read or written under it (`services/sync.py`).

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import validate_path
from syncintosh.core.paths import collision_key

# The marker that sets the logical element apart from a real release folder.
CONFIG_DIR_MARK: Final = "@"

# A prefix is the part of the folder name before the release: letters and digits, starting with a letter.
_PREFIX: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_BUNDLE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")
_GLOB_CHARACTERS: Final = ("*", "?")


@dataclass(frozen=True)
class ConfigDirPath:
    """A logical path taken apart: the file's place inside the product's config directory."""

    relative: str


@dataclass(frozen=True)
class ConfigDirSpec:
    """How one app names its versioned config directory, as data.

    * `home` — the directory that holds one folder per release (`Library/Application Support/JetBrains`).
    * `prefix` — the folder name before the release (`DataGrip`); the real folder is `<prefix><year>.<n>`.
    * `vendor` — what the app's own metadata must say it is published by; a folder name taken from an app that
      says otherwise is not trusted.
    * `bundle_id` — the app whose metadata names the folder.
    """

    home: str
    prefix: str
    vendor: str
    bundle_id: str

    @classmethod
    def create(cls, *, home: str, prefix: str, vendor: str, bundle_id: str) -> ConfigDirSpec:
        """Validate and freeze the layout; never assign fields afterwards."""
        try:
            validated_home = validate_path(home)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid config directory layout: {exc}") from exc
        if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None:
            raise ConfigError(f"a config directory prefix must be letters and digits, got {prefix!r}")
        if not isinstance(vendor, str) or vendor == "":
            raise ConfigError("a config directory layout must name its vendor")
        if not isinstance(bundle_id, str) or _BUNDLE_ID.fullmatch(bundle_id) is None:
            raise ConfigError(f"a config directory layout needs a bundle id, got {bundle_id!r}")
        return cls(home=validated_home, prefix=prefix, vendor=vendor, bundle_id=bundle_id)

    @property
    def root(self) -> str:
        """The logical directory every record of this layout lives under."""
        return f"{self.home}/{CONFIG_DIR_MARK}{self.prefix}"

    def logical_path(self, relative: str) -> str:
        """The record path for `relative` inside the product's config directory."""
        return f"{self.root}/{validate_path(relative)}"

    def contains(self, path: str) -> bool:
        """True when `path` names something under the logical root, glob characters allowed (a preset's pattern)."""
        return path.startswith(self.root + "/") and len(path) > len(self.root) + 1

    def relative_directory(self, directory: str) -> str | None:
        """The part of a logical **directory** below the root (`""` for the root itself), or `None`.

        The root is matched exactly, not folded: a logical path is only written by this code, so a differently
        cased spelling is not ours, and accepting it would let a record choose the case `apply` creates.
        """
        if directory == self.root:
            return ""
        if not directory.startswith(self.root + "/"):
            return None
        relative = directory[len(self.root) + 1 :]
        try:
            validate_path(relative)
        except ManifestCorrupt:
            return None
        return None if any(character in relative for character in _GLOB_CHARACTERS) else relative

    def parse(self, path: str) -> ConfigDirPath | None:
        """Take a logical file path apart, or `None` when `path` is not a well-formed one of this layout."""
        relative = self.relative_directory(path)
        return None if not relative else ConfigDirPath(relative=relative)

    def is_release_name(self, name: str) -> bool:
        """True when `name` is a release folder of this product: `<prefix><year>.<n>` and nothing else.

        One path element by construction (no `/`, no `..`), so a name taken from an app's metadata can only point
        to a folder directly inside `home`.
        """
        return (
            isinstance(name, str)
            and re.fullmatch(re.escape(self.prefix) + r"\d{4}\.\d+", name, flags=re.ASCII) is not None
        )

    def release_key(self, name: str) -> tuple[int, int]:
        """The `(year, n)` of a valid release name, for ordering installs. Raises when `name` is not one."""
        if not self.is_release_name(name):
            raise ConfigError(f"not a release folder name: {name!r}")
        year, _, number = name[len(self.prefix) :].partition(".")
        return int(year), int(number)

    def real_directory(self, name: str) -> str:
        """The real `$HOME`-relative directory of the release `name` — the one place `home` and a name are joined."""
        if not self.is_release_name(name):
            raise ConfigError(f"not a release folder name: {name!r}")
        return f"{self.home}/{name}"

    def owns_real(self, path: str) -> bool:
        """True when `path` lies in a real release folder of this product (a path no logical record may share)."""
        parts = [collision_key(part) for part in path.split("/")]
        home = [collision_key(part) for part in self.home.split("/")]
        pattern = re.escape(collision_key(self.prefix)) + r"\d{4}\.\d+"
        return (
            len(parts) > len(home)
            and parts[: len(home)] == home
            and re.fullmatch(pattern, parts[len(home)], flags=re.ASCII) is not None
        )


@dataclass(frozen=True)
class ConfigDirMap:
    """What this machine's installed app says its config directory is, or why that is not known.

    `directory` is the release folder name (`DataGrip2026.2`), already validated against the layout, or `None`
    with the sentence in `reason`. A map that is not resolved is never a deletion: the caller keeps its records and
    defers (``docs/design/03-manifest.md``). `problems` are notes that do not stop a resolve (several installs).
    `settings_sync_enabled` is `True` only when the app is read to have its own settings sync turned on.
    """

    directory: str | None
    reason: str = ""
    problems: tuple[str, ...] = ()
    settings_sync_enabled: bool = False

    @classmethod
    def resolved(
        cls, directory: str, problems: tuple[str, ...] = (), *, settings_sync_enabled: bool = False
    ) -> ConfigDirMap:
        """The map for an app whose config directory is `directory`."""
        if not isinstance(directory, str) or directory == "":
            raise ConfigError("a resolved config directory must be named")
        return cls(directory=directory, problems=tuple(problems), settings_sync_enabled=settings_sync_enabled)

    @classmethod
    def unresolved(cls, reason: str, problems: tuple[str, ...] = ()) -> ConfigDirMap:
        """The map for an app that is absent or whose metadata cannot be trusted: nothing resolves."""
        return cls(directory=None, reason=reason, problems=tuple(problems))


__all__ = [
    "CONFIG_DIR_MARK",
    "ConfigDirMap",
    "ConfigDirPath",
    "ConfigDirSpec",
]
