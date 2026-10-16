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

from syncintosh.core.config import LocalItem, SyncConfig
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
    return SyncConfig.create(remote=parsed.get("remote"), machine_id=parsed.get("machine_id"), items=items)


def config_to_mapping(config: SyncConfig) -> dict[str, Any]:
    """The plain nested mapping `config.json` is written as."""
    items: list[dict[str, Any]] = []
    for item in config.items:
        entry: dict[str, Any] = {
            "path": item.path,
            "preset": item.preset,
            "scope": item.scope,
            "format": item.format,
        }
        if item.machine is not None:
            entry["machine"] = item.machine
        items.append(entry)
    return {"remote": config.remote, "machine_id": config.machine_id, "items": items}


def save_config(config: SyncConfig, path: Path | None = None) -> None:
    """Write `config.json` (human-readable; it is not compared for byte stability)."""
    resolved = path if path is not None else MachinePaths.default().config_file
    resolved.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(config_to_mapping(config), indent=2, ensure_ascii=False, sort_keys=True)
    resolved.write_text(text + "\n", encoding="utf-8")


__all__ = ["config_to_mapping", "load_config", "save_config"]
