# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application use-cases.

`services` coordinates `core` over the ports it declares. It never imports a concrete
infrastructure class — `cli` injects the implementation (``AGENTS.md`` §2.2).
"""

from __future__ import annotations

from syncintosh.services.diff import DiffReport, FileDiff, diff_repo
from syncintosh.services.doctor import Check, DoctorReport, doctor
from syncintosh.services.keys import (
    export_ring,
    import_ring,
    init_new_repo,
    prune_generation,
    published_commitments,
    remote_commitments,
    rotate_repo,
)
from syncintosh.services.merge_service import RenderedMerge, merge_text
from syncintosh.services.repo_writer import write_repo
from syncintosh.services.setup import mint_machine_id, new_machine_config
from syncintosh.services.sync import Deferral, Refusal, SyncReport, sync
from syncintosh.services.watch import watch

__all__ = [
    "Check",
    "Deferral",
    "DiffReport",
    "DoctorReport",
    "FileDiff",
    "Refusal",
    "RenderedMerge",
    "SyncReport",
    "diff_repo",
    "doctor",
    "export_ring",
    "import_ring",
    "init_new_repo",
    "merge_text",
    "mint_machine_id",
    "new_machine_config",
    "prune_generation",
    "published_commitments",
    "remote_commitments",
    "rotate_repo",
    "sync",
    "watch",
    "write_repo",
]
