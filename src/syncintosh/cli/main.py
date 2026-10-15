# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `syncintosh` command-line entry point.

The binary is `syncintosh` (alias `snt`). Commands stay thin: parse, delegate to a service,
render. All decision-making is in `core`/`services` (``AGENTS.md`` §2.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from syncintosh.services import merge_documents

app = typer.Typer(
    help="Keep developer-environment settings in step across Macs.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """syncintosh — keep developer-environment settings in step across Macs.

    A callback keeps the app a real command group. Without it Typer collapses a
    single-command app, so `syncintosh merge …` would treat `merge` as the first argument.
    """


def _load(path: Path) -> dict[str, Any]:
    """Read a JSON object from ``path``; a bad file is a usage error, not a crash."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"cannot read JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path} must contain a JSON object")
    return data


@app.command()
def merge(base: Path, local: Path, remote: Path) -> None:
    """Key-level three-way merge of three JSON files. Exits 1 when conflicts remain."""
    result = merge_documents(_load(base), _load(local), _load(remote))
    typer.echo(json.dumps(result.merged, indent=2, ensure_ascii=False, sort_keys=True))
    if result.has_conflicts:
        for conflict in result.conflicts:
            typer.echo(f"conflict: {conflict.key}", err=True)
        raise typer.Exit(code=1)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
