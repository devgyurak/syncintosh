# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: the configuration a fresh `init` writes, so the policy is not in `cli/`.

The one tracked item, its scope and format, and the machine identity are domain choices, not
argument parsing; keeping them here lets a second front-end reuse them (``AGENTS.md`` §2.3).
"""

from __future__ import annotations

from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.ports import FileIdSource


def mint_machine_id(source: FileIdSource) -> str:
    """A fresh, random machine identity.

    Minted through the `FileIdSource` port so `services` never reaches for OS entropy itself
    (``AGENTS.md`` §2.1).
    """
    return source.new_file_id()


def new_machine_config(*, remote: str, machine_id: str, path: str, preset: str) -> SyncConfig:
    """The configuration for a first `init`: one common-scope, text item.

    The real preset catalog is #6; until it lands the tracked path is named directly
    (``docs/design/presets.md``).
    """
    item = LocalItem.create(path=path, preset=preset, scope="common", format="text")
    return SyncConfig.create(remote=remote, machine_id=machine_id, items=(item,))


__all__ = ["mint_machine_id", "new_machine_config"]
