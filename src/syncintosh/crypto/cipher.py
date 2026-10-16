# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `Cipher` port over the stored-object envelope and a key ring.

`services` sees only `seal`/`open`; the generation key is resolved from the `KeyStore` here, so
a caller never handles a raw key or nonce (``AGENTS.md`` §2.2, ``docs/design/04-envelope-v1.md``).
"""

from __future__ import annotations

from syncintosh.core.errors import UnknownKeyGeneration
from syncintosh.core.ports import Domain, KeyStore
from syncintosh.crypto.envelope import decrypt, encrypt


class EnvelopeCipher:
    """Seal and open envelopes using the generation keys held by `keys`."""

    def __init__(self, keys: KeyStore) -> None:
        self._keys = keys

    def seal(self, plaintext: bytes, *, domain: Domain, file_id: str, key_id: int) -> bytes:
        """Encrypt `plaintext` under generation `key_id` (resolved from the ring)."""
        key = self._keys.get(key_id)
        if key is None:
            raise UnknownKeyGeneration(key_id)
        return encrypt(plaintext, file_id=file_id, key=key, key_id=key_id, domain=domain)

    def open(self, blob: bytes, *, domain: Domain, file_id: str) -> bytes:
        """Decrypt `blob`, reading the generation from its header."""
        return decrypt(blob, file_id=file_id, keys=self._keys, domain=domain)
