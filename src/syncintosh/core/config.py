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
class SyncConfig:
    """This machine's sync configuration."""

    remote: str
    machine_id: str
    items: tuple[LocalItem, ...]

    @classmethod
    def create(cls, *, remote: str, machine_id: str, items: tuple[LocalItem, ...]) -> SyncConfig:
        """Validate the machine-wide fields and that every machine-scope item names this machine."""
        if not isinstance(remote, str) or remote == "":
            raise ConfigError("remote must be a non-empty string")
        if not isinstance(machine_id, str) or machine_id == "":
            raise ConfigError("machine_id must be a non-empty string")
        foreign = sorted(item.path for item in items if item.scope == "machine" and item.machine != machine_id)
        if foreign:
            raise ConfigError(f"machine-scope items must name this machine ({machine_id}): {foreign}")
        return cls(remote=remote, machine_id=machine_id, items=tuple(items))


def allows_path(config: SyncConfig, *, scope: Scope, machine: str | None, path: str) -> bool:
    """True when the local allowlist names this `(scope, machine, path)`.

    The comparison folds the path the same way tracking does, so two spellings of one file agree.
    `allows` is this over a record; a caller holding the fields separately — a merge task, which has
    no record until its content is merged — goes through here too, so there is one rule
    (``AGENTS.md`` §7).
    """
    wanted = (scope, machine, collision_key(path))
    return any((item.scope, item.machine, collision_key(item.path)) == wanted for item in config.items)


def allows(config: SyncConfig, record: ManifestRecord) -> bool:
    """True when the local allowlist permits writing `record`'s path on this machine.

    A record's `path` arrives from the repo, so `apply` may only write a path this machine
    already tracks — never one that arrived from the remote alone
    (``docs/design/03-manifest.md``, and note 03's "user-added path" open item).
    """
    return allows_path(config, scope=record.scope, machine=record.machine, path=record.path)


__all__ = ["LocalItem", "SyncConfig", "allows", "allows_path"]
