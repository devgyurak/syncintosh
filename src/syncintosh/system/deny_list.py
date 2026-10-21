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

The comparison is **folded** (NFC + casefold), the same way path identity is folded everywhere
else (``core.paths``). macOS's default APFS is case-insensitive, so ``~/.SSH/id_ed25519`` names
the very same file as ``~/.ssh/id_ed25519`` — and `os.path.realpath` does not normalize the case
it is given, so a case-sensitive comparison here is a bypass, not a nicety.
"""

from __future__ import annotations

import os
from pathlib import Path

from syncintosh.core.paths import collision_key

# Relative to `$HOME`. A directory entry denies everything beneath it.
DENIED_PATHS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".kube/config",
    "Library/Cookies",
    "Library/Caches",
    ".cache",
    # Docker context TLS material: a client certificate and its private key, next to the
    # context that uses them. `AGENTS.md` §3.4 names it, and the Docker preset never tracks
    # this path — the entry exists so a user-added item cannot pull it in either.
    #
    # Deliberately not listed yet: `.docker/certs.d` and `.docker/tls` (Docker's registry-CA
    # locations) and the `ca.pem`/`cert.pem`/`key.pem` defaults the CLI reference documents. None
    # existed on the inspected machine, so no preset or item can reach them today; the private-key
    # secret scan keeps a key out of the repo if a user adds one by hand. A follow-up adds them
    # when a real setup needs it (docs/design/presets.md, the `docker` notes).
    ".docker/contexts/tls",
)


def _under(candidate: str, root: str) -> bool:
    """True when `candidate` is `root` or lies beneath it, folded for case and normalization."""
    folded_candidate = collision_key(candidate)
    folded_root = collision_key(root)
    return folded_candidate == folded_root or folded_candidate.startswith(folded_root + "/")


def is_denied(path: Path, home: Path) -> bool:
    """True when `path` is a dangerous path that must never be collected.

    `path` is the live path to read; `home` is the user's home directory. Both the real path and
    the path as written are compared, so neither a symlink into a denied tree nor a denied tree
    replaced by a symlink gets through.
    """
    real_home: Path = Path(os.path.realpath(home))
    real_candidate = os.path.realpath(path)
    # `abspath`, not `realpath`: this is the path as written, so a denied directory that is
    # itself a symlink is still recognised by its name.
    written_candidate = os.path.abspath(path)  # noqa: PTH100 — resolve() would follow the link we must detect
    return any(
        _under(real_candidate, os.path.realpath(real_home / denied))
        or _under(written_candidate, str(real_home / denied))
        for denied in DENIED_PATHS
    )


__all__ = ["DENIED_PATHS", "SystemDenyList", "is_denied"]


class SystemDenyList:
    """The `DenyList` port over `is_denied` and a home-escape check."""

    def is_denied(self, home: str, relpath: str) -> bool:
        """True when `home/relpath` is a dangerous path that must never be collected."""
        home_path = Path(home)
        return is_denied(home_path / relpath, home_path)

    def escapes(self, home: str, relpath: str) -> bool:
        """True when the record's target leaves `$HOME` once symlinks are resolved.

        The **parent directory** is resolved first, so a symlinked directory cannot be used to
        write outside the home tree; the final element is `apply`'s own symlink check
        (``docs/design/03-manifest.md``).
        """
        real_home = Path(os.path.realpath(home))
        parent = Path(os.path.realpath(Path(home) / relpath)).parent
        return parent != real_home and real_home not in parent.parents
