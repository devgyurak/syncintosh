# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the AES-256-GCM envelope (``docs/design/04-envelope-v1.md``).

The blob layout, the AAD bytes, and the failure split are all fixed contracts, so this file
pins them three ways: a golden fixture (``tests/fixtures/envelope_vectors.json``), an
independent recomputation with raw ``cryptography`` primitives, and property tests over the
round-trip and over arbitrary tampering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from hypothesis import given
from hypothesis import strategies as st

import syncintosh.crypto.envelope as envelope
from syncintosh.core.errors import (
    AuthenticationError,
    CryptoError,
    UnknownKeyGeneration,
    UnsupportedEnvelopeVersion,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "envelope_vectors.json").read_text(encoding="utf-8"))
KEY = bytes.fromhex(FIXTURE["generation_key"])
KEY_ID = FIXTURE["key_id"]
FILE_ID = FIXTURE["vectors"]["object"]["file_id"]
VECTORS = FIXTURE["vectors"]
REJECTIONS = FIXTURE["rejections"]
ERRORS = {"AuthenticationError": AuthenticationError, "UnknownKeyGeneration": UnknownKeyGeneration}

# 32 lowercase hex characters, the only shape an object `file_id` may take.
HEX32 = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)

# The five byte regions of a blob: flipping any one must be rejected, and each fails for its
# own reason (a bad format, a missing key, or a broken tag).
FLIP_CASES = [
    ("version", 0, UnsupportedEnvelopeVersion),
    ("key_id", 1, UnknownKeyGeneration),
    ("nonce", 5, AuthenticationError),
    ("ciphertext", 17, AuthenticationError),
    ("tag", -1, AuthenticationError),
]


def _raw_blob(vector: dict, key: bytes, key_id: int) -> tuple[bytes, bytes]:
    """Recompute `(aad, blob)` with cryptography's primitives, not with this package."""
    header = bytes([1]) + key_id.to_bytes(4, "big")
    subject = vector["file_id"].encode("ascii")
    aad = b"".join(
        len(field).to_bytes(4, "big") + field for field in (vector["domain"].encode("ascii"), header, subject)
    )
    subkey = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=key_id.to_bytes(4, "big"),
        info=b"syncintosh/v1/" + vector["domain"].encode("ascii"),
    ).derive(key)
    nonce = bytes.fromhex(vector["nonce"])
    ciphertext = AESGCM(subkey).encrypt(nonce, bytes.fromhex(vector["plaintext_hex"]), aad)
    return aad, header + nonce + ciphertext


def test_encrypt_then_decrypt_returns_the_plaintext(memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})

    # when
    blob = envelope.encrypt(b"hello", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object")

    # then
    assert envelope.decrypt(blob, file_id=FILE_ID, keys=store, domain="object") == b"hello"


def test_manifest_roundtrip_uses_the_empty_subject(memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})

    # when
    blob = envelope.encrypt(b'{"schema":1}', file_id="", key=KEY, key_id=KEY_ID, domain="manifest")

    # then
    assert envelope.decrypt(blob, file_id="", keys=store, domain="manifest") == b'{"schema":1}'


def test_header_is_version_then_big_endian_key_id(monkeypatch) -> None:
    # given
    monkeypatch.setattr(envelope, "_random_nonce", lambda: bytes(12))

    # when
    blob = envelope.encrypt(b"x", file_id=FILE_ID, key=KEY, key_id=0x01020304, domain="object")

    # then
    assert blob[0] == envelope.VERSION
    assert blob[1:5] == b"\x01\x02\x03\x04"
    assert blob[5:17] == bytes(12)
    assert len(blob) == 5 + 12 + 1 + envelope.TAG_SIZE


@pytest.mark.parametrize(("region", "index", "error"), FLIP_CASES, ids=[case[0] for case in FLIP_CASES])
def test_a_flipped_byte_region_is_rejected(region, index, error, memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})
    blob = bytearray(envelope.encrypt(b"payload", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object"))
    blob[index] ^= 0x01

    # when & then
    with pytest.raises(error):
        envelope.decrypt(bytes(blob), file_id=FILE_ID, keys=store, domain="object")


def test_a_key_id_moved_to_a_held_generation_fails_authentication(memory_key_store) -> None:
    # given — the ring holds generation 2, so the lookup succeeds and only the tag can fail
    store = memory_key_store({KEY_ID: KEY, 2: KEY[::-1]})
    blob = bytearray(envelope.encrypt(b"payload", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object"))
    blob[1:5] = (2).to_bytes(4, "big")

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.decrypt(bytes(blob), file_id=FILE_ID, keys=store, domain="object")


def test_an_absent_generation_is_unknown_not_an_auth_failure(memory_key_store) -> None:
    # given
    store = memory_key_store({})
    blob = envelope.encrypt(b"payload", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object")

    # when & then
    with pytest.raises(UnknownKeyGeneration):
        envelope.decrypt(blob, file_id=FILE_ID, keys=store, domain="object")


def test_a_manifest_blob_moved_to_an_object_slot_fails(memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})
    blob = envelope.encrypt(b'{"schema":1}', file_id="", key=KEY, key_id=KEY_ID, domain="manifest")

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.decrypt(blob, file_id=FILE_ID, keys=store, domain="object")


def test_an_object_opened_with_another_file_id_fails(memory_key_store) -> None:
    # given — the ID is part of the AAD, so a slot move must not authenticate
    store = memory_key_store({KEY_ID: KEY})
    blob = envelope.encrypt(b"payload", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object")

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.decrypt(blob, file_id="f" * 32, keys=store, domain="object")


def test_a_blob_shorter_than_the_minimum_is_rejected(memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.decrypt(bytes(envelope.MIN_BLOB_SIZE - 1), file_id=FILE_ID, keys=store, domain="object")


def test_generation_zero_is_rejected_as_an_auth_failure(memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})
    blob = bytearray(envelope.encrypt(b"payload", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object"))
    blob[1:5] = bytes(4)

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.decrypt(bytes(blob), file_id=FILE_ID, keys=store, domain="object")


def test_build_aad_length_prefixes_every_field() -> None:
    # given
    header = b"\x01\x00\x00\x00\x01"

    # when
    aad = envelope.build_aad("object", header, b"ab")

    # then
    assert aad == b"\x00\x00\x00\x06object" + b"\x00\x00\x00\x05" + header + b"\x00\x00\x00\x02ab"


@pytest.mark.parametrize("bad_file_id", ["", "short", "F" * 32, "g" * 32, "0f3c4b5a69788796a5b4c3d2e1f0abc"])
def test_an_object_file_id_must_be_32_lowercase_hex(bad_file_id) -> None:
    # when & then
    with pytest.raises(ValueError):
        envelope.encrypt(b"x", file_id=bad_file_id, key=KEY, key_id=KEY_ID, domain="object")


def test_a_manifest_subject_must_be_empty() -> None:
    # when & then
    with pytest.raises(ValueError):
        envelope.encrypt(b"x", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="manifest")


def test_the_key_wrap_domain_is_not_encryptable_until_the_wrap_slice() -> None:
    # when & then
    with pytest.raises(ValueError):
        envelope.encrypt(b"x", file_id="", key=KEY, key_id=KEY_ID, domain="key-wrap")


def test_nonces_are_unique_across_many_encryptions() -> None:
    # when
    nonces = {envelope.encrypt(b"x", file_id=FILE_ID, key=KEY, key_id=KEY_ID, domain="object")[5:17] for _ in range(64)}

    # then
    assert len(nonces) == 64


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_fixture_blobs_recompute_with_raw_cryptography(name) -> None:
    # given
    vector = VECTORS[name]

    # when
    aad, blob = _raw_blob(vector, KEY, KEY_ID)

    # then
    assert aad.hex() == vector["aad"]
    assert blob.hex() == vector["blob"]


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_build_aad_matches_the_vectors(name) -> None:
    # given
    vector = VECTORS[name]
    header = bytes.fromhex(vector["blob"])[:5]

    # when
    aad = envelope.build_aad(vector["domain"], header, vector["file_id"].encode("ascii"))

    # then
    assert aad.hex() == vector["aad"]


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_encrypt_reproduces_the_fixture_blob(monkeypatch, name) -> None:
    # given
    vector = VECTORS[name]
    monkeypatch.setattr(envelope, "_random_nonce", lambda vec=vector: bytes.fromhex(vec["nonce"]))

    # when
    blob = envelope.encrypt(
        bytes.fromhex(vector["plaintext_hex"]),
        file_id=vector["file_id"],
        key=KEY,
        key_id=KEY_ID,
        domain=vector["domain"],
    )

    # then
    assert blob.hex() == vector["blob"]


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_fixture_blobs_decrypt_to_the_pinned_plaintext(memory_key_store, name) -> None:
    # given
    vector = VECTORS[name]
    store = memory_key_store({KEY_ID: KEY})

    # when
    plaintext = envelope.decrypt(
        bytes.fromhex(vector["blob"]), file_id=vector["file_id"], keys=store, domain=vector["domain"]
    )

    # then
    assert plaintext.hex() == vector["plaintext_hex"]


@pytest.mark.parametrize("case", REJECTIONS, ids=[case["name"] for case in REJECTIONS])
def test_the_pinned_rejection_vectors_raise(memory_key_store, case) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})

    # when & then
    with pytest.raises(ERRORS[case["error"]]):
        envelope.decrypt(bytes.fromhex(case["blob"]), file_id=case["file_id"], keys=store, domain=case["domain"])


@given(plaintext=st.binary(max_size=256), file_id=HEX32)
def test_arbitrary_plaintext_and_file_id_roundtrip(plaintext, file_id, memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})

    # when
    blob = envelope.encrypt(plaintext, file_id=file_id, key=KEY, key_id=KEY_ID, domain="object")

    # then
    assert envelope.decrypt(blob, file_id=file_id, keys=store, domain="object") == plaintext


@given(plaintext=st.binary(max_size=64), file_id=HEX32, data=st.data())
def test_any_single_byte_flip_always_raises(plaintext, file_id, data, memory_key_store) -> None:
    # given
    store = memory_key_store({KEY_ID: KEY})
    blob = bytearray(envelope.encrypt(plaintext, file_id=file_id, key=KEY, key_id=KEY_ID, domain="object"))
    position = data.draw(st.integers(min_value=0, max_value=len(blob) - 1))
    blob[position] ^= 0x01

    # when & then
    with pytest.raises(CryptoError):
        envelope.decrypt(bytes(blob), file_id=file_id, keys=store, domain="object")
