# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stored-object envelope: AES-256-GCM with an authenticated header.

The layout, the AAD, and the per-failure split are fixed by ``docs/design/04-envelope-v1.md``.
This module is the only writer of ``objects/<id>.enc``, ``manifest.enc``, and
``keys/<generation>.wrap``: the first two share the ``object`` / ``manifest`` domains, and the
wrap uses the ``key-wrap`` domain through `wrap_generation` / `unwrap_generation`.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from syncintosh.core.errors import AuthenticationError, UnknownKeyGeneration, UnsupportedEnvelopeVersion
from syncintosh.core.ids import is_file_id
from syncintosh.core.ports import Domain, KeyStore
from syncintosh.crypto.hkdf import derive_subkey
from syncintosh.crypto.primitives import KEY_ID_SIZE, KEY_SIZE, NONCE_SIZE, TAG_SIZE, be32, from_be32, length_prefixed

VERSION = 1
HEADER_SIZE = 1 + KEY_ID_SIZE  # version(1) + key_id(4)
# An empty plaintext still costs a header, a nonce, and a tag — anything shorter is not an
# envelope at all, so it fails as an authentication error rather than a version error.
MIN_BLOB_SIZE = HEADER_SIZE + NONCE_SIZE + TAG_SIZE


def build_aad(domain: Domain, header: bytes, subject: bytes) -> bytes:
    """Length-prefixed (domain, header, subject), in that order.

    `encrypt` and `decrypt` both call this, which is what makes the slot binding real rather
    than asserted.
    """
    return length_prefixed(domain.encode("ascii"), header, subject)


def pack_header(key_id: int) -> bytes:
    """`version || key_id` — the first five bytes, authenticated as part of the AAD."""
    return bytes([VERSION]) + be32(key_id)


def _resolve_subject(domain: Domain, file_id: str) -> bytes:
    """Validate `file_id` against `domain` and return the AAD `subject`."""
    if domain == "object":
        if not is_file_id(file_id):
            raise ValueError(f"an object file_id must be 32 lowercase hex characters, got {file_id!r}")
        return file_id.encode("ascii")
    if domain == "manifest":
        if file_id != "":
            raise ValueError("the manifest slot has exactly one subject; file_id must be ''")
        return b""
    raise ValueError("the key-wrap domain has no file_id; use wrap_generation()/unwrap_generation()")


def _random_nonce() -> bytes:
    """A fresh random nonce per encryption (never a counter). Tests patch this for vectors."""
    return os.urandom(NONCE_SIZE)


def encrypt(plaintext: bytes, *, file_id: str, key: bytes, key_id: int, domain: Domain) -> bytes:
    """Seal `plaintext` for `domain`, binding it to `file_id` and generation `key_id`.

    `key` is the whole generation key `KG_key_id`; the per-domain subkey is derived here, so a
    caller never handles a subkey directly.
    """
    subject = _resolve_subject(domain, file_id)
    header = pack_header(key_id)
    subkey = derive_subkey(key, key_id, domain)
    nonce = _random_nonce()
    aad = build_aad(domain, header, subject)
    ciphertext = AESGCM(subkey).encrypt(nonce, plaintext, aad)
    return header + nonce + ciphertext


def decrypt(blob: bytes, *, file_id: str, keys: KeyStore, domain: Domain) -> bytes:
    """Open `blob` for `domain`, raising a distinct error per failure mode.

    The order matters: shape, then format, then the caller's arguments, then key availability,
    then authenticity. Validating the subject before the key lookup keeps a caller bug (a bad
    `file_id`) from being masked as `UnknownKeyGeneration`.
    """
    if len(blob) < MIN_BLOB_SIZE:
        raise AuthenticationError(f"envelope is shorter than the {MIN_BLOB_SIZE}-byte minimum")
    version = blob[0]
    if version != VERSION:
        raise UnsupportedEnvelopeVersion(f"envelope version {version} is not supported by this build")
    key_id = from_be32(blob[1:HEADER_SIZE])
    if key_id == 0:
        raise AuthenticationError("generation 0 is not a valid key id")
    subject = _resolve_subject(domain, file_id)
    key = keys.get(key_id)
    if key is None:
        raise UnknownKeyGeneration(key_id)
    header = blob[:HEADER_SIZE]
    nonce = blob[HEADER_SIZE : HEADER_SIZE + NONCE_SIZE]
    ciphertext = blob[HEADER_SIZE + NONCE_SIZE :]
    aad = build_aad(domain, header, subject)
    try:
        return AESGCM(derive_subkey(key, key_id, domain)).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise AuthenticationError("envelope failed authentication (tag or AAD mismatch)") from exc


def wrap_generation(key: bytes, *, key_id: int, under: bytes, under_id: int) -> bytes:
    """Seal generation `key_id`'s key under generation `under_id`.

    The header names the **wrapper** generation (`under_id`), whose `K_wrap` subkey decrypts the
    blob; the AAD subject is `be32(key_id)`, the generation being distributed, so a wrap for one
    generation can never be presented as another's (``docs/design/05-key-generations.md``). The
    same envelope layout and AAD composition as an object apply, with domain `key-wrap`.
    """
    if len(key) != KEY_SIZE:
        raise ValueError(f"a generation key must be {KEY_SIZE} bytes, got {len(key)}")
    header = pack_header(under_id)
    subject = be32(key_id)
    nonce = _random_nonce()
    aad = build_aad("key-wrap", header, subject)
    ciphertext = AESGCM(derive_subkey(under, under_id, "key-wrap")).encrypt(nonce, key, aad)
    return header + nonce + ciphertext


def unwrap_generation(blob: bytes, *, key_id: int, keys: KeyStore) -> bytes:
    """Open `keys/<key_id>.wrap`, resolving the wrapper generation from the blob header.

    The caller supplies the generation being distributed (`key_id`, from the filename); the
    wrapper generation is read from the header and looked up in `keys`. The order matches
    `decrypt`: shape, format, then key availability, then authenticity.
    """
    if len(blob) < MIN_BLOB_SIZE:
        raise AuthenticationError(f"key wrap is shorter than the {MIN_BLOB_SIZE}-byte minimum")
    version = blob[0]
    if version != VERSION:
        raise UnsupportedEnvelopeVersion(f"envelope version {version} is not supported by this build")
    under_id = from_be32(blob[1:HEADER_SIZE])
    if under_id == 0:
        raise AuthenticationError("generation 0 is not a valid key id")
    under = keys.get(under_id)
    if under is None:
        raise UnknownKeyGeneration(under_id)
    header = blob[:HEADER_SIZE]
    nonce = blob[HEADER_SIZE : HEADER_SIZE + NONCE_SIZE]
    ciphertext = blob[HEADER_SIZE + NONCE_SIZE :]
    aad = build_aad("key-wrap", header, be32(key_id))
    try:
        key = AESGCM(derive_subkey(under, under_id, "key-wrap")).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise AuthenticationError("key wrap failed authentication (tag or AAD mismatch)") from exc
    if len(key) != KEY_SIZE:
        # A valid wrap always carries a 32-byte generation key, so a wrong width is not a key.
        raise AuthenticationError(f"key wrap did not carry a {KEY_SIZE}-byte generation key")
    return key
