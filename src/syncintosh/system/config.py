# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading and writing `config.json`.

`pydantic-settings.BaseSettings()` reads environment variables and files the moment it is
instantiated, so it must be constructed only inside a factory (``AGENTS.md`` §2.6). This module
is that factory boundary: `load_config` does the I/O and returns a frozen `SyncConfig` built by
its single `create` classmethod, and a missing or malformed file is a `ConfigError`, never a
bare exception.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from syncintosh.core.config import NO_LLM, LlmConfig, LocalGlob, LocalItem, SyncConfig
from syncintosh.core.errors import ConfigError
from syncintosh.system.paths import MachinePaths

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


def load_config(path: Path | None = None) -> SyncConfig:
    """Read and validate `config.json`, defaulting to this machine's config path."""
    resolved = path if path is not None else MachinePaths.default().config_file
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
    items_raw = parsed.get("items")
    if not isinstance(items_raw, list):
        raise ConfigError(f"config {resolved} must hold an `items` list")
    items = tuple(_parse_item(raw, f"items[{index}]") for index, raw in enumerate(items_raw))
    globs_raw = parsed.get("globs", [])
    if not isinstance(globs_raw, list):
        raise ConfigError(f"config {resolved} must hold a `globs` list")
    globs = tuple(_parse_glob(raw, f"globs[{index}]") for index, raw in enumerate(globs_raw))
    llm = _parse_llm(parsed.get("llm"), resolved)
    return SyncConfig.create(
        remote=parsed.get("remote"), machine_id=parsed.get("machine_id"), items=items, globs=globs, llm=llm
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
    if config.llm != NO_LLM:
        # Written only when set, so a config that never used the feature keeps its exact shape.
        mapping["llm"] = _llm_mapping(config.llm)
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
    """Write `config.json` (human-readable; it is not compared for byte stability)."""
    resolved = path if path is not None else MachinePaths.default().config_file
    resolved.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(config_to_mapping(config), indent=2, ensure_ascii=False, sort_keys=True)
    resolved.write_text(text + "\n", encoding="utf-8")


__all__ = ["config_to_mapping", "load_config", "save_config"]
