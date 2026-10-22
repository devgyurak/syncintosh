# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a ring exported on one machine decrypts an object on another.

The key hand-off flow (``docs/design/05-key-generations.md``): machine A builds a ring, seals
an object, and exports the ring to a file; machine B imports what it unwraps and opens the
same object. It runs under the temp HOME from ``conftest`` and an in-memory keyring backend,
so neither the real keychain nor the real HOME is ever touched (``AGENTS.md`` §5.6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.crypto.envelope import decrypt, encrypt
from syncintosh.crypto.keyring_store import KeyringKeyStore
from syncintosh.crypto.ring_export import Ring, RingEntry, unwrap_ring, wrap_ring

pytestmark = pytest.mark.integration

KEY = bytes(range(32))
FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
PLAINTEXT = b"export EDITOR=vim\n"
PASSPHRASE = "a recovery passphrase"


class FakeKeyringBackend:
    """A machine's own keyring: two instances are two independent machines."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._entries.pop((service, username), None)


def _dump(store: KeyringKeyStore) -> Ring:
    """The `(generation, key)` entries an export carries; a held generation is never absent."""
    return tuple(RingEntry(gid, key) for gid in store.generations() if (key := store.get(gid)) is not None)


def _load(store: KeyringKeyStore, ring: Ring) -> None:
    for entry in ring:
        store.put(entry.key_id, entry.key)


def test_a_ring_exported_on_one_machine_decrypts_on_another() -> None:
    # given — machine A holds generation 1 and seals an object with it
    machine_a = KeyringKeyStore("syncintosh:repo", FakeKeyringBackend())
    machine_a.put(1, KEY)
    blob = encrypt(PLAINTEXT, file_id=FILE_ID, key=KEY, key_id=1, domain="object")

    # when — A exports its ring to a file under the temp HOME, and B imports it
    bundle = Path.home() / "ring-export.sntr"
    bundle.write_bytes(wrap_ring(_dump(machine_a), PASSPHRASE))
    machine_b = KeyringKeyStore("syncintosh:repo", FakeKeyringBackend())
    _load(machine_b, unwrap_ring(bundle.read_bytes(), PASSPHRASE))

    # then — the imported ring opens the very object A sealed
    assert machine_b.generations() == (1,)
    assert decrypt(blob, file_id=FILE_ID, keys=machine_b, domain="object") == PLAINTEXT
