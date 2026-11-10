# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: the sync cycle for the tracked text files.

The 14 steps of ``docs/design/01-sync-model.md``, in order: lock → collect → scan → stage →
commit → fetch → classify → base → decrypt → merge → gate → publish → retry → apply. The rules
that shape the code:

* **Change detection is against the apply ledger, never `HEAD`**, because apply runs after the
  push (``core/ledger.py``).
* **Git never merges ciphertext.** Sync decrypts both sides, merges plaintext, and builds the
  result with one `commit-tree` holding two parents.
* **Conflicts never touch live files.** A conflict writes `pending/`, leaves the local commit in
  place, and stops without pushing.
* **Publish before apply**, and apply checks every write against its baseline (compare-and-swap).
* **Commit only on change**, so an unchanged sync rewrites nothing.
* **A preset's policy is applied on the collect side.** The bytes that are hashed, staged,
  merged, and sealed are the *projection* — the file with the preset's noise and excluded keys
  removed — and apply re-attaches the local values from the live file
  (``docs/design/02-base-and-merge.md``, "Projection"). A noise-only change therefore reaches
  neither the digest nor the merge, and a Docker `auths` block is never sealed because it was
  never collected (``AGENTS.md`` §3.4).

`services` orchestrates the ports; it never imports `vcs`, `crypto`, `system`, or `adapters`
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

from syncintosh.services.sync._shared import (
    MAX_PUSH_ATTEMPTS,
    NO_APP_INFO,
    NO_CATALOG,
    NO_RUNNING_INFO,
    NO_SESSION_ACTIVITY,
    NO_SESSION_STORE,
    NO_VALIDATORS,
    Backup,
    Deferral,
    DroppedLocal,
    RecordReader,
    RestorePoint,
    SyncPorts,
    SyncReport,
)
from syncintosh.services.sync.cycle import sync
from syncintosh.services.sync.merge import NO_RESOLUTION, Resolution

__all__ = [
    "MAX_PUSH_ATTEMPTS",
    "NO_APP_INFO",
    "NO_CATALOG",
    "NO_RESOLUTION",
    "NO_RUNNING_INFO",
    "NO_SESSION_ACTIVITY",
    "NO_SESSION_STORE",
    "NO_VALIDATORS",
    "Backup",
    "Deferral",
    "DroppedLocal",
    "RecordReader",
    "Resolution",
    "RestorePoint",
    "SyncPorts",
    "SyncReport",
    "sync",
]
