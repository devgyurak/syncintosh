# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `LiveFiles` over the real filesystem, addressed by absolute path.

The collection step reads the user's files through this port and `apply` writes them, so a path
from an untrusted manifest is never opened by `services` directly — the caller resolves it under
`$HOME` first and the port only performs the byte movement (``docs/design/03-manifest.md``).

A write is atomic: a crash mid-write must not leave a truncated live settings file. `is_symlink`
exists so `apply` can refuse to write through a link and report it instead of silently breaking
a dotfile setup (``docs/design/03-manifest.md`` "Open").
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class SystemLiveFiles:
    """Read and write the user's real files."""

    def read(self, path: str) -> bytes | None:
        """Return the file's bytes, or `None` when it does not exist."""
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, path: str, data: bytes) -> None:
        """Write `data` to `path` atomically, creating parent directories."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def exists(self, path: str) -> bool:
        """True when `path` exists (a symlink counts as existing)."""
        return Path(path).is_symlink() or Path(path).exists()

    def is_symlink(self, path: str) -> bool:
        """True when `path` is a symbolic link."""
        return Path(path).is_symlink()

    def remove(self, path: str) -> None:
        """Remove the file if present. A missing file is not a failure of intent."""
        Path(path).unlink(missing_ok=True)


__all__ = ["SystemLiveFiles"]
