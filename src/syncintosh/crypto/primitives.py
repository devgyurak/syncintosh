# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared low-level shapes for the crypto layer.

The byte formats the design notes fix — the big-endian `uint32`, the length-prefixed AAD
framing, and the AES-GCM sizes — live here once, so the envelope, the key derivation, and the
ring export cannot drift apart (``docs/design/04-envelope-v1.md``).
"""

from __future__ import annotations

KEY_SIZE = 32
KEY_ID_SIZE = 4
NONCE_SIZE = 12
TAG_SIZE = 16

UINT32_MAX = 0xFFFF_FFFF


def be32(value: int) -> bytes:
    """A 4-byte big-endian `uint32` — a header id, an HKDF salt, or a length prefix.

    An out-of-range value is a caller bug, so it raises `ValueError` rather than surfacing as
    an `OverflowError` deeper in a conversion.
    """
    if not 0 <= value <= UINT32_MAX:
        raise ValueError(f"a uint32 must be 0..{UINT32_MAX}, got {value}")
    return value.to_bytes(KEY_ID_SIZE, "big")


def length_prefixed(*fields: bytes) -> bytes:
    """`be32(len(field)) || field` for each field — the AAD framing every slot uses.

    Length prefixes keep each tuple of fields mapped to exactly one byte string; plain
    concatenation would let two different tuples collide.
    """
    return b"".join(be32(len(field)) + field for field in fields)
