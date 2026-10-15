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

KEY_SIZE = 32
INFO_PREFIX = b"syncintosh/v1/"


def key_id_bytes(key_id: int) -> bytes:
    """The 4-byte big-endian generation id — the HKDF salt and the header `key_id`."""
    return key_id.to_bytes(4, "big")


def hkdf_info(domain: Domain) -> bytes:
    """The HKDF `info` for `domain`; distinct per purpose, so the subkeys are independent."""
    return INFO_PREFIX + domain.encode("ascii")


def derive_subkey(generation_key: bytes, key_id: int, domain: Domain) -> bytes:
    """Derive the 32-byte AES-256 subkey for `domain` under generation `key_id`.

    A wrong-size key or a non-positive id is a caller bug, not a crypto failure, so it raises
    `ValueError` rather than a `CryptoError`.
    """
    if key_id < 1:
        raise ValueError(f"generation id must be >= 1, got {key_id}")
    if len(generation_key) != KEY_SIZE:
        raise ValueError(f"generation key must be {KEY_SIZE} bytes, got {len(generation_key)}")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=key_id_bytes(key_id),
        info=hkdf_info(domain),
    ).derive(generation_key)
