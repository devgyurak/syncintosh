# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `Cipher` and `GenerationWrap` ports over the stored-object envelope and a key ring.

`services` sees only `seal`/`open` and `wrap`/`unwrap`; the generation key is resolved from the
`KeyStore` here, so a caller never handles a raw key or nonce (``AGENTS.md`` §2.2,
``docs/design/04-envelope-v1.md``).
"""

from __future__ import annotations

from syncintosh.core.errors import UnknownKeyGeneration
from syncintosh.core.ports import Domain, KeyStore
from syncintosh.crypto.envelope import decrypt, encrypt, unwrap_generation, wrap_generation


class EnvelopeCipher:
    """Seal and open envelopes using the generation keys held by `keys`.

    It also implements `GenerationWrap`: a rotation publishes `keys/<new_id>.wrap`, and a
    machine that holds the wrapper generation absorbs the new one through `unwrap`.
    """

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

    def wrap(self, key: bytes, *, key_id: int, under_id: int) -> bytes:
        """Seal generation `key_id`'s key under `under_id`, resolved from the ring."""
        under = self._keys.get(under_id)
        if under is None:
            raise UnknownKeyGeneration(under_id)
        return wrap_generation(key, key_id=key_id, under=under, under_id=under_id)

    def unwrap(self, blob: bytes, *, key_id: int) -> bytes:
        """Open `keys/<key_id>.wrap`, resolving the wrapper generation from the header."""
        return unwrap_generation(blob, key_id=key_id, keys=self._keys)
