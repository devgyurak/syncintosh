# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `LiveFiles` over the real filesystem, addressed by absolute path.

The collection step reads the user's files through this port and `apply` writes them, so a path
from an untrusted manifest is never opened by `services` directly — the caller resolves it under
`$HOME` first and the port only performs the byte movement (``docs/design/03-manifest.md``).

A write is atomic and preserves the file's mode: a crash mid-write must not leave a truncated
live settings file, and an applied file must not silently lose its permissions or its executable
bit. The rename that makes the write atomic does sever a hard link at the path — that trade is
deliberate, because overwriting in place is what leaves a truncated file behind a crash.
`is_symlink` exists so `apply` can refuse to write through a link and report it instead of
silently breaking a dotfile setup (``docs/design/03-manifest.md`` "Open").
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from syncintosh.system.durability import flush_to_storage, sync_directory


def _default_mode() -> int:
    """The mode a brand-new file gets: the umask-adjusted default `mkstemp` would not apply.

    `mkstemp` creates the temp file `0600`, so without this a newly created settings file would
    be readable only by its owner where the shell would have made it `0644`.
    """
    current = os.umask(0o022)
    os.umask(current)
    return 0o666 & ~current


def _target_mode(target: Path) -> int:
    """The mode to give the replacement: the file's own, or the umask default when it is new."""
    try:
        return stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        return _default_mode()


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
        mode = _target_mode(target)
        descriptor, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                flush_to_storage(handle.fileno())
            Path(tmp_name).chmod(mode)
            Path(tmp_name).replace(target)
            sync_directory(target.parent)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def exists(self, path: str) -> bool:
        """True when `path` exists (a symlink counts as existing)."""
        return Path(path).is_symlink() or Path(path).exists()

    def is_symlink(self, path: str) -> bool:
        """True when `path` is a symbolic link."""
        return Path(path).is_symlink()

    def list_dir(self, path: str) -> tuple[str, ...]:
        """The last elements of the non-directory entries in `path`, sorted.

        A symbolic link is included even when it points at a directory, so the collector can see
        and report it rather than silently drop it. A subdirectory is excluded: a glob matches the
        last element only and never recurses (`core/presets.PresetGlob`). So is anything that is
        neither a regular file nor a symlink — a FIFO or a socket is not a settings file, and a
        blocking read of one would hang the collect. A path that is missing or is not a directory
        answers empty, the same way `read` answers `None` for a missing file.
        """
        try:
            entries = list(Path(path).iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return ()
        return tuple(sorted(entry.name for entry in entries if entry.is_symlink() or entry.is_file()))

    def remove(self, path: str) -> None:
        """Remove the file if present. A missing file is not a failure of intent."""
        Path(path).unlink(missing_ok=True)


__all__ = ["SystemLiveFiles"]
