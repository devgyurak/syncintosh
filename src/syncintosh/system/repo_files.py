# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `RepoFiles` over a directory tree — the repo's git working tree.

Paths are repo-relative and are only ever built by ``core.repo`` (``objects/<id>.enc``,
``manifest.enc``), so a real path from the manifest never reaches the filesystem here
(``AGENTS.md`` §3.2).
"""

from __future__ import annotations

from pathlib import Path


class FileRepo:
    """Read and write repo files beneath `root`."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _resolve(self, relpath: str) -> Path:
        return self._root / relpath

    def read(self, relpath: str) -> bytes | None:
        """Return the file's bytes, or `None` when it does not exist."""
        try:
            return self._resolve(relpath).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, relpath: str, data: bytes) -> None:
        """Write `data`, creating parent directories. Raises on failure."""
        path = self._resolve(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, relpath: str) -> None:
        """Remove the file if present. A missing file is not a failure of intent."""
        self._resolve(relpath).unlink(missing_ok=True)

    def list_paths(self, prefix: str) -> tuple[str, ...]:
        """Return the repo-relative paths of existing files under `prefix`, sorted."""
        base = self._resolve(prefix)
        if not base.is_dir():
            return ()
        return tuple(sorted(str(path.relative_to(self._root)) for path in base.rglob("*") if path.is_file()))
