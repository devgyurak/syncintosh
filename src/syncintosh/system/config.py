# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading and writing `config.json`.

A settings object must not read anything when it is constructed (``AGENTS.md`` §2.6). This module
is the factory boundary: `load_config` reads `config.json` and parses it with `json` directly, then
returns a frozen `SyncConfig` built by its single `create` classmethod, and a missing or malformed
file is a `ConfigError`, never a bare exception.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from syncintosh.core.agent_projects import AgentLayout, AgentProject
from syncintosh.core.config import NO_LLM, LlmConfig, LocalGlob, LocalItem, LocalProfiles, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.core.ports import PresetCatalog
from syncintosh.system.paths import MachinePaths

# Every top-level key `config.json` is written with. `config_to_mapping` writes exactly these, and a rewrite refuses
# a file holding any other, so a key this build does not model is not silently dropped (`load_config_for_rewrite`).
TOP_LEVEL_KEYS = frozenset({"remote", "machine_id", "items", "globs", "profiles", "agent_projects", "llm"})

_ITEM_KEYS = frozenset({"path", "preset", "scope", "machine", "format"})


def _parse_item(raw: object, where: str) -> LocalItem:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a JSON object")
    unknown = sorted(raw.keys() - _ITEM_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown fields: {unknown}")
    if "path" not in raw or "preset" not in raw or "scope" not in raw or "format" not in raw:
        raise ConfigError(f"{where} must name path, preset, scope, and format")
    return LocalItem.create(
        path=raw["path"],
        preset=raw["preset"],
        scope=raw["scope"],
        machine=raw.get("machine"),
        format=raw["format"],
    )


def _parse_glob(raw: object, where: str) -> LocalGlob:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a JSON object")
    unknown = sorted(raw.keys() - _ITEM_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown fields: {unknown}")
    if "path" not in raw or "preset" not in raw or "scope" not in raw or "format" not in raw:
        raise ConfigError(f"{where} must name path, preset, scope, and format")
    return LocalGlob.create(
        path=raw["path"],
        preset=raw["preset"],
        scope=raw["scope"],
        machine=raw.get("machine"),
        format=raw["format"],
    )


_PROFILES_KEYS = frozenset({"preset", "scope", "machine", "names"})


def _parse_profiles(raw: object, where: str, catalog: PresetCatalog) -> LocalProfiles:
    """One `profiles` entry. The layout comes from the built-in preset by name, never from the file."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a JSON object")
    unknown = sorted(raw.keys() - _PROFILES_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown fields: {unknown}")
    if "preset" not in raw or "scope" not in raw or "names" not in raw:
        raise ConfigError(f"{where} must name preset, scope, and names")
    preset = catalog.get(raw["preset"]) if isinstance(raw["preset"], str) else None
    if preset is None or preset.profiles is None:
        raise ConfigError(f"{where}: {raw['preset']!r} is not a built-in preset with profiles")
    names = raw["names"]
    return LocalProfiles.create(
        preset=preset.name,
        spec=preset.profiles,
        scope=raw["scope"],
        machine=raw.get("machine"),
        names="*" if names == "*" else tuple(names) if isinstance(names, list) else names,
    )


_AGENT_PROJECT_KEYS = frozenset({"name", "path", "presets"})


def _parse_agent_project(raw: object, where: str, catalog: PresetCatalog) -> AgentProject:
    """One `agent_projects` entry. The layouts come from the built-in presets by name, never from the file."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a JSON object")
    unknown = sorted(raw.keys() - _AGENT_PROJECT_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown fields: {unknown}")
    if "name" not in raw or "path" not in raw or "presets" not in raw:
        raise ConfigError(f"{where} must name name, path, and presets")
    names = raw["presets"]
    if not isinstance(names, list) or not all(isinstance(each, str) for each in names):
        raise ConfigError(f"{where}: presets must be a list of preset names")
    layouts: list[AgentLayout] = []
    for each in names:
        preset = catalog.get(each)
        if preset is None or preset.agent_projects is None:
            raise ConfigError(f"{where}: {each!r} is not a built-in preset with per-project data")
        layouts.append(AgentLayout.create(preset=preset.name, spec=preset.agent_projects))
    return AgentProject.create(name=raw["name"], path=raw["path"], layouts=layouts)


def _read_object(resolved: Path) -> dict[str, Any]:
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {resolved}: {exc}") from exc
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config {resolved} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"config {resolved} must contain a JSON object")
    return parsed


def load_config(path: Path | None = None, catalog: PresetCatalog | None = None) -> SyncConfig:
    """Read and validate `config.json`, defaulting to this machine's config path.

    `catalog` resolves a `profiles` entry's preset to its layout; it defaults to the shipped catalog, which is
    imported only when the file has such an entry, so a plain config pays nothing for it.
    """
    resolved = path if path is not None else MachinePaths.default().config_file
    return _config_from(_read_object(resolved), resolved, catalog)


def load_config_for_rewrite(path: Path | None = None, catalog: PresetCatalog | None = None) -> SyncConfig:
    """`load_config`, but refuse a file holding a top-level key this build does not model.

    `load_config` ignores such a key so an older build still reads a newer file. A rewrite would drop it, so a
    command that saves the config reads it here instead and the person decides what to do with the key.
    """
    resolved = path if path is not None else MachinePaths.default().config_file
    parsed = _read_object(resolved)
    unknown = sorted(parsed.keys() - TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"config {resolved} has keys this build would drop on rewrite: {unknown}")
    return _config_from(parsed, resolved, catalog)


def _config_from(parsed: Any, resolved: Path, catalog: PresetCatalog | None) -> SyncConfig:
    items_raw = parsed.get("items")
    if not isinstance(items_raw, list):
        raise ConfigError(f"config {resolved} must hold an `items` list")
    items = tuple(_parse_item(raw, f"items[{index}]") for index, raw in enumerate(items_raw))
    globs_raw = parsed.get("globs", [])
    if not isinstance(globs_raw, list):
        raise ConfigError(f"config {resolved} must hold a `globs` list")
    globs = tuple(_parse_glob(raw, f"globs[{index}]") for index, raw in enumerate(globs_raw))
    llm = _parse_llm(parsed.get("llm"), resolved)
    profiles_raw = parsed.get("profiles", [])
    if not isinstance(profiles_raw, list):
        raise ConfigError(f"config {resolved} must hold a `profiles` list")
    projects_raw = parsed.get("agent_projects", [])
    if not isinstance(projects_raw, list):
        raise ConfigError(f"config {resolved} must hold an `agent_projects` list")
    if (profiles_raw or projects_raw) and catalog is None:
        from syncintosh.presets import BuiltinCatalog

        catalog = BuiltinCatalog.default()
    profiles = tuple(
        _parse_profiles(raw, f"profiles[{index}]", catalog)  # ty: ignore[invalid-argument-type]  # set above when the list is non-empty
        for index, raw in enumerate(profiles_raw)
    )
    agent_projects = tuple(
        _parse_agent_project(raw, f"agent_projects[{index}]", catalog)  # ty: ignore[invalid-argument-type]  # set above when the list is non-empty
        for index, raw in enumerate(projects_raw)
    )
    return SyncConfig.create(
        remote=parsed.get("remote"),
        machine_id=parsed.get("machine_id"),
        items=items,
        globs=globs,
        llm=llm,
        profiles=profiles,
        agent_projects=agent_projects,
    )


_LLM_KEYS: frozenset[str] = frozenset({"base_url", "model", "internal_domains", "auto_apply"})


def _parse_llm(raw: object, where: object) -> LlmConfig:
    """The optional `llm` object. An unknown key is refused, so a typo cannot silently disable a guard."""
    if raw is None:
        return NO_LLM
    if not isinstance(raw, dict):
        raise ConfigError(f"config {where}: `llm` must be an object")
    unknown = sorted(set(raw) - _LLM_KEYS)
    if unknown:
        raise ConfigError(f"config {where}: unknown llm keys {unknown}")
    return LlmConfig.create(
        base_url=_optional_string(raw, "base_url", where),
        model=_optional_string(raw, "model", where),
        internal_domains=_string_list(raw, "internal_domains", where),
        auto_apply=_string_list(raw, "auto_apply", where),
    )


def _optional_string(raw: dict[str, Any], key: str, where: object) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"config {where}: llm.{key} must be a string")
    return value


def _string_list(raw: dict[str, Any], key: str, where: object) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"config {where}: llm.{key} must be a list of strings")
    return tuple(value)


def config_to_mapping(config: SyncConfig) -> dict[str, Any]:
    """The plain nested mapping `config.json` is written as."""

    def _entry(path: str, preset: str, scope: str, machine: str | None, fmt: str) -> dict[str, Any]:
        item: dict[str, Any] = {"path": path, "preset": preset, "scope": scope, "format": fmt}
        if machine is not None:
            item["machine"] = machine
        return item

    mapping: dict[str, Any] = {
        "remote": config.remote,
        "machine_id": config.machine_id,
        "items": [_entry(i.path, i.preset, i.scope, i.machine, i.format) for i in config.items],
        "globs": [_entry(g.glob.path, g.preset, g.scope, g.machine, g.format) for g in config.globs],
    }
    if config.profiles:
        # Written only when present, so a config that never tracked profiles keeps its exact shape.
        mapping["profiles"] = [_profiles_mapping(entry) for entry in config.profiles]
    if config.agent_projects:
        # Written only when present, so a config that never mapped a project keeps its exact shape.
        mapping["agent_projects"] = [
            {"name": project.name, "path": project.path, "presets": [layout.preset for layout in project.layouts]}
            for project in config.agent_projects
        ]
    if config.llm != NO_LLM:
        # Written only when set, so a config that never used the feature keeps its exact shape.
        mapping["llm"] = _llm_mapping(config.llm)
    return mapping


def _profiles_mapping(entry: LocalProfiles) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "preset": entry.preset,
        "scope": entry.scope,
        "names": entry.names if entry.names == "*" else list(entry.names),
    }
    if entry.machine is not None:
        mapping["machine"] = entry.machine
    return mapping


def _llm_mapping(llm: LlmConfig) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "base_url": llm.base_url,
        "model": llm.model,
        "internal_domains": list(llm.internal_domains),
        "auto_apply": list(llm.auto_apply),
    }
    return {key: value for key, value in fields.items() if value not in (None, [])}


def save_config(config: SyncConfig, path: Path | None = None) -> None:
    """Write `config.json` (human-readable; it is not compared for byte stability).

    Written to a temporary file beside it, flushed to disk, and renamed over it, so a crash or a full disk leaves the
    old file whole rather than a truncated or empty one that no command could then read. A config that is a symlink
    (a dotfiles checkout) is written where it points: replacing the link itself would leave the real file stale.
    A failed write is a `ConfigError`, not a traceback.
    """
    resolved = (path if path is not None else MachinePaths.default().config_file).resolve()
    text = json.dumps(config_to_mapping(config), indent=2, ensure_ascii=False, sort_keys=True)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=resolved.parent, prefix=f".{resolved.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(resolved)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ConfigError(f"cannot write config {resolved}: {exc}") from exc


__all__ = ["TOP_LEVEL_KEYS", "config_to_mapping", "load_config", "load_config_for_rewrite", "save_config"]
