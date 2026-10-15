# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports implemented by infrastructure.

`core` and `services` depend on these protocols, never on a concrete adapter, cipher, git
client, or LLM (``AGENTS.md`` §2.2). Implementations live in `adapters`, `crypto`, `vcs`,
`system`, and `llm`.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

# What an envelope binds a ciphertext to, and which subkey decrypts it
# (`docs/design/04-envelope-v1.md`). A value is serialized verbatim in the HKDF `info`, so it
# is a `Literal`, never an `Enum` (``AGENTS.md`` §4).
Domain = Literal["object", "manifest", "key-wrap"]


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
    """The per-repo key ring: generation id -> 32-byte generation key.

    Addresses generations by id, not a single master key, because `merge-base` can point at a
    commit encrypted under an older generation and several generations must resolve at once
    (`docs/design/05-key-generations.md`). `drop()` is deliberately absent here: prune and the
    removal of an unpublished rotation land with the rotation slice (#8).
    """

    def generations(self) -> tuple[int, ...]:
        """Return the stored generation ids, ascending."""
        ...

    def get(self, key_id: int) -> bytes | None:
        """Return generation `key_id`, or `None` when this machine does not hold it."""
        ...

    def put(self, key_id: int, key: bytes) -> None:
        """Store a generation key. Raises on failure."""
        ...
