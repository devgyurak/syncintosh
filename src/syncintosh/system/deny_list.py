# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dangerous paths refused before collection.

These are refused **before anything is read**, in code, not by asking the user
(``AGENTS.md`` §3.4): a private key or a cloud credential must never enter a repo, however the
user configured their presets.

The judgement is by **real path**. A symlink is the obvious bypass — a preset naming
``~/notes/link`` whose target is ``~/.ssh`` would look innocent at the path level — so both the
resolved target *and* the unresolved path under `$HOME` are checked. Resolving only the target
would miss a denied directory that is itself a symlink out of the tree; checking only the
unresolved path would miss a link pointing in.
"""

from __future__ import annotations

import os
from pathlib import Path

# Relative to `$HOME`. A directory entry denies everything beneath it.
DENIED_PATHS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".kube/config",
    "Library/Cookies",
    "Library/Caches",
    ".cache",
)


def _under(candidate: Path, root: Path) -> bool:
    """True when `candidate` is `root` or lies beneath it."""
    return candidate == root or root in candidate.parents


def is_denied(path: Path, home: Path) -> bool:
    """True when `path` is a dangerous path that must never be collected.

    `path` is the live path to read; `home` is the user's home directory. Both the real path and
    the path as written are compared, so neither a symlink into a denied tree nor a denied tree
    replaced by a symlink gets through.
    """
    real_home = Path(os.path.realpath(home))
    real_candidate = Path(os.path.realpath(path))
    # `abspath`, not `realpath`: this is the path as written, so a denied directory that is
    # itself a symlink is still recognised by its name.
    written_candidate = Path(os.path.abspath(path))  # noqa: PTH100 — resolve() would follow the link we must detect
    return any(
        _under(real_candidate, Path(os.path.realpath(real_home / denied)))
        or _under(written_candidate, real_home / denied)
        for denied in DENIED_PATHS
    )


__all__ = ["DENIED_PATHS", "is_denied"]
