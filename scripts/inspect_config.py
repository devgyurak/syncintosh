#!/usr/bin/env python3
"""Redacting configuration inspector for the add-preset workflow.

`add-preset` step 0 requires looking at a real config file, but the guard blocks reading
secret-shaped files (Docker `config.json`, anything with `auths`, keys). This script is
the **sanctioned way** to learn the shape of such a file without exposing values:

- it prints the key structure with **all values masked**,
- it never prints a value that looks like a secret,
- it only reads; it writes nothing.

Usage:
    uv run python scripts/inspect_config.py <path> [--max-depth N] [--show-types] [--shape-strings]
    uv run python scripts/inspect_config.py --sqlite-document <db> <domain> [same flags]

`--sqlite-document` reads one JSON document row (`profile_state_documents.payload` for `<domain>`) from an app's
SQLite database, the way Orca keeps its settings, and prints it through the same masking as a file: strings and the
values of secret-named keys are masked, while key names and non-string scalars (numbers, booleans) are shown, exactly
as in file mode. The database is opened **read-only** (`mode=ro`, never `immutable`, so what is still in the WAL is
seen), nothing is written, and an error names the problem but never a value.

`--shape-strings` answers one question the masked length cannot: *is this string a machine-local
absolute path, or a portable value?* It prints `<path>` / `<bool>` / `<str:N>` for the value
instead of only the length, so a preset author can tell a per-machine plugin path (noise) from a
user preference (synced) without ever seeing the value. A key that looks secret stays `<redacted>`
whatever the flag.

Running it still reads the file on disk, so it is meant for a human or an agent that the
user has authorized to inspect their own configuration. It never echoes raw values.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Names whose value must never be echoed, even masked differently.
SECRET_KEY_RE = re.compile(
    r"(auth|token|secret|passwd|password|api[_-]?key|credential|private|cookie|session|bearer)",
    re.IGNORECASE,
)
MASK = "<redacted>"


def _shape_string(value: str) -> str:
    """A string's *shape*, never its value: portable, a path, or a boolean-as-string."""
    if value.startswith(("/", "~")):
        return "<path>"
    if value in {"true", "false"}:
        return "<bool>"
    return f"<str:{len(value)}>"


def _mask_value(key: str, value: Any, shape_strings: bool = False) -> Any:
    if SECRET_KEY_RE.search(key):
        return MASK
    if isinstance(value, str):
        if shape_strings:
            return _shape_string(value)
        # Keep only the shape: a non-empty string became a short placeholder.
        return f"<str:{len(value)}>" if value else "<str:0>"
    return value


def _walk(node: Any, depth: int, max_depth: int, show_types: bool, shape_strings: bool = False) -> Any:
    if depth > max_depth:
        return "<...>"
    if isinstance(node, dict):
        return {
            key: _mask_value(key, _walk(value, depth + 1, max_depth, show_types, shape_strings), shape_strings)
            for key, value in node.items()
        }
    if isinstance(node, list):
        # A list item has no key to judge by, so it is masked by its shape alone — the name-based
        # check cannot help. Skipping this, as an earlier version did, printed array elements
        # verbatim (`insecure-registries`, a header list), which is exactly the leak this tool
        # exists to prevent.
        return [
            _mask_value("", _walk(item, depth + 1, max_depth, show_types, shape_strings), shape_strings)
            for item in node[:5]
        ] + (["<...>"] if len(node) > 5 else [])
    if show_types:
        return f"<{type(node).__name__}>"
    return node


def _load(path: Path) -> Any:
    if path.suffix == ".plist" or path.name.endswith(".plist"):
        with path.open("rb") as handle:
            return plistlib.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def _load_document(database: Path, domain: str) -> Any:
    """The JSON value of one `profile_state_documents` row, from a database opened read-only."""
    with closing(sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT payload FROM profile_state_documents WHERE domain = ?", (domain,)).fetchone()
    if row is None or not isinstance(row[0], str):
        raise ValueError("no such document")
    return json.loads(row[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a config file's structure with values masked.")
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument(
        "--sqlite-document",
        nargs=2,
        metavar=("DB", "DOMAIN"),
        help="inspect one JSON document row of an app's SQLite database instead of a file",
    )
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--show-types", action="store_true")
    parser.add_argument(
        "--shape-strings",
        action="store_true",
        help="classify strings as <path>/<bool>/<str:N> instead of only their length",
    )
    args = parser.parse_args()

    if (args.path is None) == (args.sqlite_document is None):
        parser.error("give a path, or --sqlite-document DB DOMAIN, not both")

    if args.sqlite_document is not None:
        database, domain = Path(args.sqlite_document[0]), args.sqlite_document[1]
        if not database.is_file():
            print(f"not a file: {database}", file=sys.stderr)
            return 1
        try:
            data = _load_document(database, domain)
        except (sqlite3.Error, ValueError):
            # The message of a JSON or SQLite error can quote the payload; only the class of failure is printed.
            print(f"could not read document {domain!r} from {database}", file=sys.stderr)
            return 1
        label = f"{database} [{domain}]"
    else:
        if not args.path.is_file():
            print(f"not a file: {args.path}", file=sys.stderr)
            return 1
        try:
            data = _load(args.path)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            print(f"could not parse {args.path}: {exc}", file=sys.stderr)
            return 1
        label = str(args.path)

    shape = _walk(data, 0, args.max_depth, args.show_types, args.shape_strings)
    print(f"# {label} (values masked)")
    print(json.dumps(shape, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
