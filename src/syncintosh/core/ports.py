# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports implemented by infrastructure.

`core` and `services` depend on these protocols, never on a concrete adapter, cipher, git
client, or LLM (``AGENTS.md`` §2.2). Implementations live in `adapters`, `crypto`, `vcs`,
`system`, and `llm`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MergeDriver(Protocol):
    """Line-level three-way merge.

    `merge3` and `git merge-file` are not stdlib, so they sit behind this port; `core` keeps
    only the key-level rules (``AGENTS.md`` §2.9).
    """

    def merge(self, base: str, local: str, remote: str) -> str:
        """Return the merged text, or text containing conflict markers."""
        ...


@runtime_checkable
class KeyStore(Protocol):
    """Where the per-repo master key lives (the platform keychain, in practice)."""

    def get(self) -> bytes | None:
        """Return the stored master key, or ``None`` when none is set."""
        ...

    def set(self, key: bytes) -> None:
        """Store the master key. Raises on failure."""
        ...
