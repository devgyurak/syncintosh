# SPDX-License-Identifier: AGPL-3.0-or-later
"""The machine-local configuration: the remote, this machine's ID, and the tracked items.

`config.json` holds **non-secret** values only — the remote URL, the machine ID, and the local
allowlist of paths to track (``docs/design/README.md``). It is the local allowlist of note 03's
"Open" item: a path a record may be applied to must be one this machine already knows, never one
that arrived from the remote alone.

The item shape reuses the manifest's path/preset/scope/format rules rather than copying them, so
a configuration cannot describe an item the manifest would reject. Standard library only
(``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import (
    FileFormat,
    ManifestRecord,
    validate_format,
    validate_machine,
    validate_path,
    validate_preset,
    validate_scope,
)
from syncintosh.core.models import Scope
from syncintosh.core.paths import collision_key
from syncintosh.core.presets import PresetGlob
from syncintosh.core.profiles import ProfileSpec, duplicate_profile_names, profile_name_key


@dataclass(frozen=True)
class LocalItem:
    """One path this machine tracks: where it lives, which preset owns it, and its format."""

    path: str
    preset: str
    scope: Scope
    machine: str | None
    format: FileFormat

    @classmethod
    def create(
        cls,
        *,
        path: str,
        preset: str,
        scope: Scope,
        machine: str | None = None,
        format: FileFormat,
    ) -> LocalItem:
        """Validate the item with the manifest's own rules and build it frozen."""
        try:
            validated_scope = validate_scope(scope)
            return cls(
                path=validate_path(path),
                preset=validate_preset(preset),
                scope=validated_scope,
                machine=validate_machine(machine, validated_scope),
                format=validate_format(format),
            )
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid tracked item: {exc}") from exc


@dataclass(frozen=True)
class LlmConfig:
    """The assisted-resolve settings. Nothing here is a secret: the API key comes from the environment or the keyring.

    `base_url` has no default, so an assisted resolve runs only against an endpoint the person named.
    `internal_domains` adds to the built-in internal suffixes (``core/advice.py``). `auto_apply` lists
    preset names whose content conflicts may be applied without asking; record-level conflicts and
    proposals that add a URL are always asked (``docs/design/06-llm-assist.md``).
    """

    base_url: str | None = None
    model: str | None = None
    internal_domains: tuple[str, ...] = ()
    auto_apply: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        base_url: str | None = None,
        model: str | None = None,
        internal_domains: tuple[str, ...] = (),
        auto_apply: tuple[str, ...] = (),
    ) -> LlmConfig:
        """Validate the endpoint is an http(s) URL with a host, then freeze."""
        if base_url is not None:
            parts = urlsplit(base_url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ConfigError(f"llm base_url must be an http(s) URL with a host, got {base_url!r}")
        return cls(base_url=base_url, model=model, internal_domains=internal_domains, auto_apply=auto_apply)


NO_LLM: Final = LlmConfig()


@dataclass(frozen=True)
class SyncConfig:
    """This machine's sync configuration."""

    remote: str
    machine_id: str
    items: tuple[LocalItem, ...]
    globs: tuple[LocalGlob, ...] = ()
    llm: LlmConfig = NO_LLM
    profiles: tuple[LocalProfiles, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        remote: str,
        machine_id: str,
        items: tuple[LocalItem, ...],
        globs: tuple[LocalGlob, ...] = (),
        llm: LlmConfig = NO_LLM,
        profiles: tuple[LocalProfiles, ...] = (),
    ) -> SyncConfig:
        """Validate the machine-wide fields and that every machine-scope entry names this machine."""
        if not isinstance(remote, str) or remote == "":
            raise ConfigError("remote must be a non-empty string")
        if not isinstance(machine_id, str) or machine_id == "":
            raise ConfigError("machine_id must be a non-empty string")
        foreign = sorted(
            [
                *(item.path for item in items if item.scope == "machine" and item.machine != machine_id),
                *(entry.glob.path for entry in globs if entry.scope == "machine" and entry.machine != machine_id),
                *(entry.preset for entry in profiles if entry.scope == "machine" and entry.machine != machine_id),
            ]
        )
        if foreign:
            raise ConfigError(f"machine-scope entries must name this machine ({machine_id}): {foreign}")
        return cls(
            remote=remote,
            machine_id=machine_id,
            items=tuple(items),
            globs=tuple(globs),
            llm=llm,
            profiles=tuple(profiles),
        )


@dataclass(frozen=True)
class LocalGlob:
    """One directory pattern this machine allows a record to be written under.

    The exact allowlist (`LocalItem`) cannot express a preset that owns a **directory** of per-machine
    files — VS Code's `snippets/*.json` — because the files themselves differ between Macs: this one
    has `javascript.json`, the other has `python.json`. A glob bounds the allowed set to a directory
    this machine chose, so a record that arrived from the other machine for a snippet it created is
    still writable here, without opening an arbitrary path.

    Security boundary: this is the local half of note 03's "Open" item — a record's path arrives from
    the repo, and only a glob **this machine's own config** names can widen what `apply` may touch
    (``docs/design/03-manifest.md``).
    """

    glob: PresetGlob
    preset: str
    scope: Scope
    machine: str | None
    format: FileFormat

    @classmethod
    def create(
        cls,
        *,
        path: str,
        preset: str,
        scope: Scope,
        machine: str | None = None,
        format: FileFormat,
    ) -> LocalGlob:
        """Validate the glob (and its preset/scope) with the manifest's own rules and freeze it."""
        try:
            validated_scope = validate_scope(scope)
            preset_name = validate_preset(preset)
            validated_format = validate_format(format)
            machine_id = validate_machine(machine, validated_scope)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid tracked glob: {exc}") from exc
        return cls(
            glob=PresetGlob.create(path),
            preset=preset_name,
            scope=validated_scope,
            machine=machine_id,
            format=validated_format,
        )


@dataclass(frozen=True)
class LocalProfiles:
    """The profiles of one preset this machine allows a record to be written under.

    `names` is `"*"` (every profile this machine has) or the profile names, compared the way
    `collision_key` compares. It bounds **which profiles**; it does not say a profile exists. A write
    still needs the name to resolve through this machine's own profile index (`core/profiles.py`),
    so a name that arrived from the remote can never create a directory (decision D3, D5).

    Security boundary: like `LocalGlob`, this is a local allowlist. A profile *id* from the remote is
    not an entry here and never could be, because the logical path carries a name and the real
    directory is looked up locally.
    """

    preset: str
    spec: ProfileSpec
    scope: Scope
    machine: str | None
    names: Literal["*"] | tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        preset: str,
        spec: ProfileSpec,
        scope: Scope,
        machine: str | None = None,
        names: Literal["*"] | tuple[str, ...] = "*",
    ) -> LocalProfiles:
        """Validate the entry (and its preset/scope) with the manifest's own rules and freeze it."""
        try:
            validated_scope = validate_scope(scope)
            preset_name = validate_preset(preset)
            machine_id = validate_machine(machine, validated_scope)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid tracked profiles: {exc}") from exc
        if not isinstance(spec, ProfileSpec):
            raise ConfigError(f"profiles need a layout, got {type(spec).__name__}")
        if names != "*":
            if isinstance(names, str) or not all(isinstance(name, str) and name != "" for name in names):
                raise ConfigError(f"profile names must be '*' or a list of non-empty names, got {names!r}")
            if duplicate_profile_names(names):
                raise ConfigError(f"a profile is named twice: {list(duplicate_profile_names(names))}")
            names = tuple(names)
        return cls(preset=preset_name, spec=spec, scope=validated_scope, machine=machine_id, names=names)

    def allows(self, name: str) -> bool:
        """True when this entry covers the profile called `name`."""
        return self.names == "*" or profile_name_key(name) in {profile_name_key(each) for each in self.names}


def allows_path(config: SyncConfig, *, scope: Scope, machine: str | None, path: str) -> bool:
    """True when the local allowlist names this `(scope, machine, path)`.

    An exact item matches the folded path; a glob entry matches when `path` falls in its directory.
    The comparison folds the path the same way tracking does, so two spellings of one file agree.
    `allows` is this over a record; a caller holding the fields separately — a merge task, which has
    no record until its content is merged — goes through here too, so there is one rule
    (``AGENTS.md`` §7).
    """
    folded = collision_key(path)
    if any(
        (item.scope, item.machine) == (scope, machine) and collision_key(item.path) == folded for item in config.items
    ):
        return True
    if any((entry.scope, entry.machine) == (scope, machine) and entry.glob.matches(path) for entry in config.globs):
        return True
    return any(_allows_profile(entry, scope=scope, machine=machine, path=path) for entry in config.profiles)


def _allows_profile(entry: LocalProfiles, *, scope: Scope, machine: str | None, path: str) -> bool:
    """True when `path` is a logical profile path `entry` covers: its layout, a listed name, a listed file."""
    parsed = entry.spec.parse(path)
    return parsed is not None and (entry.scope, entry.machine) == (scope, machine) and entry.allows(parsed.name)


def allows(config: SyncConfig, record: ManifestRecord) -> bool:
    """True when the local allowlist permits writing `record`'s path on this machine.

    A record's `path` arrives from the repo, so `apply` may only write a path this machine
    already allows — an exact tracked item or one inside a glob this machine's config names, never
    one that arrived from the remote alone (``docs/design/03-manifest.md``).
    """
    return allows_path(config, scope=record.scope, machine=record.machine, path=record.path)


__all__ = ["NO_LLM", "LlmConfig", "LocalGlob", "LocalItem", "LocalProfiles", "SyncConfig", "allows", "allows_path"]
