# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application use-cases.

`services` coordinates `core` over the ports it declares. It never imports a concrete
infrastructure class — `cli` injects the implementation (``AGENTS.md`` §2.2).
"""

from __future__ import annotations

from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.merge_service import merge_documents
from syncintosh.services.repo_writer import write_repo
from syncintosh.services.setup import mint_machine_id, new_machine_config
from syncintosh.services.sync import SyncReport, sync

__all__ = [
    "SyncReport",
    "import_ring",
    "init_new_repo",
    "merge_documents",
    "mint_machine_id",
    "new_machine_config",
    "sync",
    "write_repo",
]
