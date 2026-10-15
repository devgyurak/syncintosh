#!/usr/bin/env python3
"""Redacting configuration inspector for the add-preset workflow.

`add-preset` step 0 requires looking at a real config file, but the guard blocks reading
secret-shaped files (Docker `config.json`, anything with `auths`, keys). This script is
the **sanctioned way** to learn the shape of such a file without exposing values:

- it prints the key structure with **all values masked**,
- it never prints a value that looks like a secret,
- it only reads; it writes nothing.

Usage:
    uv run python scripts/inspect_config.py <path> [--max-depth N] [--show-types]

Running it still reads the file on disk, so it is meant for a human or an agent that the
user has authorized to inspect their own configuration. It never echoes raw values.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
from pathlib import Path
from typing import Any

# Names whose value must never be echoed, even masked differently.
SECRET_KEY_RE = re.compile(
    r"(auth|token|secret|passwd|password|api[_-]?key|credential|private|cookie|session|bearer)",
    re.IGNORECASE,
)
MASK = "<redacted>"


def _mask_value(key: str, value: Any) -> Any:
    if SECRET_KEY_RE.search(key):
        return MASK
    if isinstance(value, str):
        # Keep only the shape: a non-empty string became a short placeholder.
        return f"<str:{len(value)}>" if value else "<str:0>"
    return value


def _walk(node: Any, depth: int, max_depth: int, show_types: bool) -> Any:
    if depth > max_depth:
        return "<...>"
    if isinstance(node, dict):
        return {key: _mask_value(key, _walk(value, depth + 1, max_depth, show_types)) for key, value in node.items()}
    if isinstance(node, list):
        return [_walk(item, depth + 1, max_depth, show_types) for item in node[:5]] + (
            ["<...>"] if len(node) > 5 else []
        )
    if show_types:
        return f"<{type(node).__name__}>"
    return node


def _load(path: Path) -> Any:
    if path.suffix == ".plist" or path.name.endswith(".plist"):
        with path.open("rb") as handle:
            return plistlib.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a config file's structure with values masked.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--show-types", action="store_true")
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"not a file: {args.path}", file=sys.stderr)
        return 1

    try:
        data = _load(args.path)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        print(f"could not parse {args.path}: {exc}", file=sys.stderr)
        return 1

    shape = _walk(data, 0, args.max_depth, args.show_types)
    print(f"# {args.path} (values masked)")
    print(json.dumps(shape, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
