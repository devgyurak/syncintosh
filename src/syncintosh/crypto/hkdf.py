# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-generation, per-domain subkeys (HKDF-SHA256).

`KG_g` is the generation key; each purpose derives its own subkey. An object key can then
never decrypt a manifest, even if the AAD could somehow be satisfied
(``docs/design/04-envelope-v1.md``).
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from syncintosh.core.ports import Domain
from syncintosh.crypto.primitives import KEY_SIZE, be32

INFO_PREFIX = b"syncintosh/v1/"


def hkdf_info(domain: Domain) -> bytes:
    """The HKDF `info` for `domain`; distinct per purpose, so the subkeys are independent."""
    return INFO_PREFIX + domain.encode("ascii")


def derive_subkey(generation_key: bytes, key_id: int, domain: Domain) -> bytes:
    """Derive the 32-byte AES-256 subkey for `domain` under generation `key_id`.

    A wrong-size key or a non-positive id is a caller bug, not a crypto failure, so it raises
    `ValueError` rather than a `CryptoError`. The salt is `be32(key_id)`, which also rejects an
    id above `2**32 - 1`.
    """
    if key_id < 1:
        raise ValueError(f"generation id must be >= 1, got {key_id}")
    if len(generation_key) != KEY_SIZE:
        raise ValueError(f"generation key must be {KEY_SIZE} bytes, got {len(generation_key)}")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=be32(key_id),
        info=hkdf_info(domain),
    ).derive(generation_key)
