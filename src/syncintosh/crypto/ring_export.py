# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ring export: the whole key ring wrapped under a passphrase.

The blob layout is fixed by ``docs/design/05-key-generations.md`` ("Ring export"). It is
self-describing so an import can reject an unknown format or a KDF it will not run *before*
spending work on the derivation — a tampered blob must not be able to force a slow KDF.
"""

from __future__ import annotations

import os
import struct
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from syncintosh.core.errors import MasterKeyUnwrapError
from syncintosh.crypto.primitives import KEY_ID_SIZE, KEY_SIZE, NONCE_SIZE, TAG_SIZE, be32, from_be32, length_prefixed

MAGIC = b"SNTR"
FORMAT_VERSION = 1
KDF_SCRYPT = 1
SALT_SIZE = 16
AAD_DOMAIN = b"ring-export"

# magic | format | kdf | log2_n | r | p | salt. One definition of the header layout for both
# pack and unpack, so the two can never disagree on an offset.
_HEADER = struct.Struct(">4sBBBII16s")
HEADER_SIZE = _HEADER.size
MIN_BLOB_SIZE = HEADER_SIZE + NONCE_SIZE + TAG_SIZE


@dataclass(frozen=True)
class RingEntry:
    """One generation in an export: its id and its 32-byte key.

    `repr` masks the key, so an unwrapped ring cannot leak into a log or a traceback
    (``AGENTS.md`` §4).
    """

    key_id: int
    key: bytes

    def __repr__(self) -> str:
        return f"RingEntry(key_id={self.key_id}, key=<{len(self.key)} bytes masked>)"


# The whole ring, ascending by generation id.
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
    """Length-prefixed ("ring-export", header) — its own domain, so the blob cannot be
    mistaken for a stored object or a key wrap."""
    return length_prefixed(AAD_DOMAIN, header)


def _derive_kek(passphrase: str, salt: bytes, params: ScryptParams) -> bytes:
    """The AES-256 key-encryption key for `passphrase`.

    The passphrase is **NFC-normalized** before UTF-8 encoding: the same text typed in NFC and
    pasted from an NFD source must derive one key, or a valid recovery passphrase could read as
    "wrong". The export format is a contract, so this can never be loosened later.
    """
    normalized = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    return Scrypt(salt=salt, length=KEY_SIZE, n=params.n, r=params.r, p=params.p).derive(normalized)


def _pack_ring(ring: Iterable[RingEntry]) -> bytes:
    """`count(be32) || (key_id(be32) || KG(32))*`, ascending, so `KG_1` comes first."""
    entries = sorted(ring, key=lambda entry: entry.key_id)
    if not entries or entries[0].key_id != 1:
        raise ValueError("a ring must hold generation 1 and no lower id")
    if len({entry.key_id for entry in entries}) != len(entries):
        raise ValueError("a ring cannot hold two keys for one generation id")
    if any(len(entry.key) != KEY_SIZE for entry in entries):
        raise ValueError(f"every generation key must be {KEY_SIZE} bytes")
    body = b"".join(be32(entry.key_id) + entry.key for entry in entries)
    return be32(len(entries)) + body


def _unpack_ring(plaintext: bytes) -> Ring:
    if len(plaintext) < 4:
        raise MasterKeyUnwrapError("ring export plaintext is truncated")
    count = from_be32(plaintext[:4])
    body = plaintext[4:]
    if count == 0 or len(body) != count * (KEY_ID_SIZE + KEY_SIZE):
        raise MasterKeyUnwrapError("ring export plaintext does not match its record count")
    entries = tuple(
        RingEntry(
            from_be32(body[start : start + KEY_ID_SIZE]),
            body[start + KEY_ID_SIZE : start + KEY_ID_SIZE + KEY_SIZE],
        )
        for start in range(0, len(body), KEY_ID_SIZE + KEY_SIZE)
    )
    # The plaintext is authenticated, so a violation is a bug, not an attack — but a partially
    # loaded ring must not reach the store, so it fails here as one error.
    if [entry.key_id for entry in entries] != sorted({entry.key_id for entry in entries}) or entries[0].key_id != 1:
        raise MasterKeyUnwrapError("ring export is not an ascending ring of unique generations starting at 1")
    return entries


def wrap_ring(
    ring: Iterable[RingEntry],
    passphrase: str,
    *,
    params: ScryptParams = PRODUCTION_SCRYPT,
    allowed: frozenset[ScryptParams] = PRODUCTION_ALLOWED,
) -> bytes:
    """Wrap the whole ring as one export blob, sealed under `passphrase`.

    `params` must be in `allowed`, the same allowlist `unwrap_ring` enforces, so an export can
    never be written that this build would refuse to open.
    """
    if params not in allowed:
        raise ValueError("refusing to wrap the ring with KDF parameters this build will not unwrap")
    plaintext = _pack_ring(ring)
    salt = os.urandom(SALT_SIZE)
    header = _HEADER.pack(MAGIC, FORMAT_VERSION, KDF_SCRYPT, params.log2_n, params.r, params.p, salt)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(_derive_kek(passphrase, salt, params)).encrypt(nonce, plaintext, _build_aad(header))
    return header + nonce + ciphertext


def unwrap_ring(blob: bytes, passphrase: str, *, allowed: frozenset[ScryptParams] = PRODUCTION_ALLOWED) -> Ring:
    """Unwrap an export blob into an ascending `Ring`.

    The KDF runs **only** when the blob's serialized parameters are in `allowed`; anything
    else is refused first, so a tampered blob cannot force an expensive derivation (DoS) and a
    deliberately weak export is rejected. A wrong passphrase and a damaged blob are the same
    `MasterKeyUnwrapError`.
    """
    if len(blob) < MIN_BLOB_SIZE:
        raise MasterKeyUnwrapError(f"ring export is shorter than the {MIN_BLOB_SIZE}-byte minimum")
    magic, format_version, kdf, log2_n, r, p, salt = _HEADER.unpack_from(blob)
    if magic != MAGIC:
        raise MasterKeyUnwrapError("ring export does not begin with the SNTR magic")
    if format_version != FORMAT_VERSION:
        raise MasterKeyUnwrapError(f"ring export format {format_version} is not supported")
    if kdf != KDF_SCRYPT:
        raise MasterKeyUnwrapError(f"ring export KDF {kdf} is not supported")
    try:
        params = ScryptParams(log2_n=log2_n, r=r, p=p)
    except ValueError as exc:
        raise MasterKeyUnwrapError("ring export carries invalid scrypt parameters") from exc
    if params not in allowed:
        raise MasterKeyUnwrapError("ring export uses KDF parameters this build will not run")
    nonce = blob[HEADER_SIZE : HEADER_SIZE + NONCE_SIZE]
    ciphertext = blob[HEADER_SIZE + NONCE_SIZE :]
    try:
        plaintext = AESGCM(_derive_kek(passphrase, salt, params)).decrypt(
            nonce, ciphertext, _build_aad(blob[:HEADER_SIZE])
        )
    except InvalidTag as exc:
        raise MasterKeyUnwrapError("ring export failed to unwrap (wrong passphrase or damaged blob)") from exc
    return _unpack_ring(plaintext)
