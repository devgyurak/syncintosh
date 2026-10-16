# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filesystem durability barriers shared by the repo and live-file writers.

An atomic write is three steps — write a temp file, fsync it, rename it into place — and the
rename itself is a directory metadata change that needs its own fsync. Both writers need exactly
these two barriers, so they live here once rather than being copied and drifting apart.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


def flush_to_storage(descriptor: int) -> None:
    """Push a file's data to the physical device where the platform can.

    On macOS `fsync` only asks for the data to be queued; `F_FULLFSYNC` is the barrier that
    survives a power loss. It is best-effort — some filesystems reject it — so a refusal falls
    back to the plain `fsync` already issued rather than failing the write.
    """
    full_sync = getattr(fcntl, "F_FULLFSYNC", None)
    if full_sync is None:
        return
    try:
        fcntl.fcntl(descriptor, full_sync)
    except OSError:
        return


def sync_directory(path: Path) -> None:
    """fsync a directory so a rename into it is durable.

    `os.fsync` on the file makes the *data* durable, but the directory entry created by the
    rename is a separate metadata change; without this a crash right after the write can still
    lose the whole file.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["flush_to_storage", "sync_directory"]
