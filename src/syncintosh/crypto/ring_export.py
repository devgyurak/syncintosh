# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ring export: the whole key ring wrapped under a passphrase.

The blob layout is fixed by ``docs/design/05-key-generations.md`` ("Ring export"). It is
self-describing so an import can reject an unknown format or a KDF it will not run *before*
spending work on the derivation — a tampered blob must not be able to force a slow KDF.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from syncintosh.core.errors import MasterKeyUnwrapError

MAGIC = b"SNTR"
FORMAT_VERSION = 1
KDF_SCRYPT = 1
SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16
KEY_SIZE = 32
KEY_ID_SIZE = 4
HEADER_SIZE = len(MAGIC) + 3 + 4 + 4 + SALT_SIZE  # magic | format | kdf | log2_n | r | p | salt
MIN_BLOB_SIZE = HEADER_SIZE + NONCE_SIZE + TAG_SIZE
AAD_DOMAIN = b"ring-export"

# A single ring entry: a generation id and its 32-byte key.
RingEntry = tuple[int, bytes]
Ring = tuple[RingEntry, ...]


@dataclass(frozen=True)
class ScryptParams:
    """The scrypt work factor, carried verbatim in the export header.

    `log2_n` is serialized in one byte, so it is the exponent (17 = 128 MiB with r=8), never
    `N` itself. Frozen and hashable, so it can be an allowlist member.
    """

    log2_n: int
    r: int
    p: int

    def __post_init__(self) -> None:
        if not 1 <= self.log2_n <= 255:
            raise ValueError(f"log2_n must be 1..255, got {self.log2_n}")
        if not 0 < self.r <= 0xFFFF_FFFF:
            raise ValueError(f"r must be a positive uint32, got {self.r}")
        if not 0 < self.p <= 0xFFFF_FFFF:
            raise ValueError(f"p must be a positive uint32, got {self.p}")

    @property
    def n(self) -> int:
        """The scrypt cost `N = 2^log2_n`."""
        return 1 << self.log2_n


# Production KDF parameters (``AGENTS.md`` §3.1). Never reduced for tests: a test that wants
# a cheap KDF passes its own `params`, so the shipped default cannot be weakened by accident.
PRODUCTION_SCRYPT = ScryptParams(log2_n=17, r=8, p=1)
PRODUCTION_ALLOWED = frozenset({PRODUCTION_SCRYPT})


def _build_aad(header: bytes) -> bytes:
    """`be32(len) || field` for ("ring-export", header) — its own domain, so it cannot be
    mistaken for a stored object or a key wrap."""
    fields = (AAD_DOMAIN, header)
    return b"".join(len(field).to_bytes(4, "big") + field for field in fields)


def _derive_kek(passphrase: str, salt: bytes, params: ScryptParams) -> bytes:
    return Scrypt(salt=salt, length=KEY_SIZE, n=params.n, r=params.r, p=params.p).derive(passphrase.encode("utf-8"))


def _pack_ring(ring: Iterable[RingEntry]) -> bytes:
    """`count(be32) || (key_id(be32) || KG(32))*`, ascending, so `KG_1` comes first."""
    pairs = sorted(ring)
    if not pairs or pairs[0][0] != 1:
        raise ValueError("a ring must hold generation 1 and no lower id")
    if len({key_id for key_id, _ in pairs}) != len(pairs):
        raise ValueError("a ring cannot hold two keys for one generation id")
    if any(len(key) != KEY_SIZE for _, key in pairs):
        raise ValueError(f"every generation key must be {KEY_SIZE} bytes")
    body = b"".join(key_id.to_bytes(KEY_ID_SIZE, "big") + key for key_id, key in pairs)
    return len(pairs).to_bytes(4, "big") + body


def _unpack_ring(plaintext: bytes) -> Ring:
    if len(plaintext) < 4:
        raise MasterKeyUnwrapError("ring export plaintext is truncated")
    count = int.from_bytes(plaintext[:4], "big")
    body = plaintext[4:]
    if count == 0 or len(body) != count * (KEY_ID_SIZE + KEY_SIZE):
        raise MasterKeyUnwrapError("ring export plaintext does not match its record count")
    return tuple(
        (
            int.from_bytes(body[start : start + KEY_ID_SIZE], "big"),
            body[start + KEY_ID_SIZE : start + KEY_ID_SIZE + KEY_SIZE],
        )
        for start in range(0, len(body), KEY_ID_SIZE + KEY_SIZE)
    )


def wrap_ring(ring: Iterable[RingEntry], passphrase: str, *, params: ScryptParams = PRODUCTION_SCRYPT) -> bytes:
    """Wrap the whole ring as one export blob, sealed under `passphrase`."""
    plaintext = _pack_ring(ring)
    salt = os.urandom(SALT_SIZE)
    header = (
        MAGIC
        + bytes([FORMAT_VERSION, KDF_SCRYPT, params.log2_n])
        + params.r.to_bytes(4, "big")
        + params.p.to_bytes(4, "big")
        + salt
    )
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(_derive_kek(passphrase, salt, params)).encrypt(nonce, plaintext, _build_aad(header))
    return header + nonce + ciphertext


def unwrap_ring(blob: bytes, passphrase: str, *, allowed: frozenset[ScryptParams] = PRODUCTION_ALLOWED) -> Ring:
    """Unwrap an export blob into `((key_id, key), ...)`, ascending.

    The KDF runs **only** when the blob's serialized parameters are in `allowed`; anything
    else is refused first, so a tampered blob cannot force an expensive derivation (DoS) and a
    deliberately weak export is rejected. A wrong passphrase and a damaged blob are the same
    `MasterKeyUnwrapError`.
    """
    if len(blob) < MIN_BLOB_SIZE:
        raise MasterKeyUnwrapError(f"ring export is shorter than the {MIN_BLOB_SIZE}-byte minimum")
    if blob[: len(MAGIC)] != MAGIC:
        raise MasterKeyUnwrapError("ring export does not begin with the SNTR magic")
    if blob[len(MAGIC)] != FORMAT_VERSION:
        raise MasterKeyUnwrapError(f"ring export format {blob[len(MAGIC)]} is not supported")
    if blob[len(MAGIC) + 1] != KDF_SCRYPT:
        raise MasterKeyUnwrapError(f"ring export KDF {blob[len(MAGIC) + 1]} is not supported")
    try:
        params = ScryptParams(
            log2_n=blob[len(MAGIC) + 2],
            r=int.from_bytes(blob[len(MAGIC) + 3 : len(MAGIC) + 7], "big"),
            p=int.from_bytes(blob[len(MAGIC) + 7 : len(MAGIC) + 11], "big"),
        )
    except ValueError as exc:
        raise MasterKeyUnwrapError("ring export carries invalid scrypt parameters") from exc
    if params not in allowed:
        raise MasterKeyUnwrapError("ring export uses KDF parameters this build will not run")
    salt = blob[len(MAGIC) + 11 : HEADER_SIZE]
    nonce = blob[HEADER_SIZE : HEADER_SIZE + NONCE_SIZE]
    ciphertext = blob[HEADER_SIZE + NONCE_SIZE :]
    try:
        plaintext = AESGCM(_derive_kek(passphrase, salt, params)).decrypt(
            nonce, ciphertext, _build_aad(blob[:HEADER_SIZE])
        )
    except InvalidTag as exc:
        raise MasterKeyUnwrapError("ring export failed to unwrap (wrong passphrase or damaged blob)") from exc
    return _unpack_ring(plaintext)
