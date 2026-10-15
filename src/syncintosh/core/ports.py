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


@runtime_checkable
class FileIdSource(Protocol):
    """Mints the stable, random, fixed-width ID a newly tracked path keeps forever.

    Randomness is the OS entropy pool, which `core` may not touch, so the source lives in
    `crypto` (``AGENTS.md`` §2.1, §2.2). The port is intentionally tiny: `core` needs one fresh
    ID per new `(scope, machine, path)` and revalidates it (`docs/design/03-manifest.md`).
    """

    def new_file_id(self) -> str:
        """Return a fresh 32-lowercase-hex file ID."""
        ...


@runtime_checkable
class Cipher(Protocol):
    """Seals and opens stored objects under the current key generation.

    `services` never touches a raw key or nonce: it hands a plaintext to `seal` and gets the
    envelope bytes back (``docs/design/04-envelope-v1.md``). `open` reads the generation from
    the blob header, so only `seal` takes `key_id` explicitly.
    """

    def seal(self, plaintext: bytes, *, domain: Domain, file_id: str, key_id: int) -> bytes:
        """Encrypt `plaintext` for `domain`, binding it to `file_id` and generation `key_id`."""
        ...

    def open(self, blob: bytes, *, domain: Domain, file_id: str) -> bytes:
        """Decrypt `blob`, raising a distinct error per failure mode."""
        ...


@runtime_checkable
class RepoFiles(Protocol):
    """Reads and writes files in the repo's working tree, addressed by repo-relative path."""

    def read(self, relpath: str) -> bytes | None:
        """Return the file's bytes, or `None` when it does not exist."""
        ...

    def write(self, relpath: str, data: bytes) -> None:
        """Write `data`, creating parent directories. Raises on failure."""
        ...

    def delete(self, relpath: str) -> None:
        """Remove the file if present. Raises on any other failure."""
        ...

    def list_paths(self, prefix: str) -> tuple[str, ...]:
        """Return the repo-relative paths of existing files under `prefix`, sorted."""
        ...


@runtime_checkable
class SecretScanner(Protocol):
    """The secret scan that gates the encrypt boundary.

    Nothing reaches `Cipher.seal` without passing this (``docs/design/01-sync-model.md``,
    ``AGENTS.md`` §3.4). The implementation lands with #7; until then only tests provide one,
    and no no-op default exists (decision 3 of the #3 plan).
    """

    def scan(self, plaintext: bytes, *, subject: str) -> None:
        """Raise `SecretScanError` when a secret is found; return `None` when clean."""
        ...
