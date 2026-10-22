# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `key-wrap` envelope (``docs/design/05-key-generations.md``).

A hygiene rotation distributes `KG_{n+1}` sealed under `KG_n`: the header names the **wrapper**
generation, and the AAD subject is `be32(n+1)`, the generation being distributed. The blob is an
ordinary envelope with the `key-wrap` domain, so the byte layout, the tamper split, and the
domain separation all hold exactly as they do for an object.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import syncintosh.crypto.envelope as envelope
from syncintosh.core.errors import (
    AuthenticationError,
    UnknownKeyGeneration,
    UnsupportedEnvelopeVersion,
)
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.primitives import KEY_SIZE

GEN1 = bytes(range(32))
GEN2 = bytes(range(32, 64))
GEN3 = bytes(range(64, 96))
FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def test_wrap_then_unwrap_returns_the_distributed_key(memory_key_store) -> None:
    # given — the reader holds only the wrapper generation
    store = memory_key_store({1: GEN1})

    # when — KG_2 is sealed under KG_1
    blob = envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1)

    # then
    assert envelope.unwrap_generation(blob, key_id=2, keys=store) == GEN2


def test_the_header_names_the_wrapper_generation() -> None:
    # given — the blob is sealed under generation 1
    blob = envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1)

    # when
    header_key_id = int.from_bytes(blob[1:5], "big")

    # then — the header is the wrapper, never the distributed generation (which is the subject)
    assert header_key_id == 1
    assert blob[0] == envelope.VERSION


def test_unwrapping_without_the_wrapper_generation_is_unknown(memory_key_store) -> None:
    # given — the reader does not hold KG_1
    store = memory_key_store({2: GEN2})
    blob = envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1)

    # when & then
    with pytest.raises(UnknownKeyGeneration):
        envelope.unwrap_generation(blob, key_id=2, keys=store)


def test_a_wrap_presented_as_another_generation_fails(memory_key_store) -> None:
    # given — the subject binds the wrap to the generation it distributes
    store = memory_key_store({1: GEN1})
    blob = envelope.wrap_generation(GEN3, key_id=3, under=GEN1, under_id=1)

    # when & then — opening it as generation 2 must not authenticate
    with pytest.raises(AuthenticationError):
        envelope.unwrap_generation(blob, key_id=2, keys=store)


def test_an_object_blob_is_not_a_key_wrap(memory_key_store) -> None:
    # given — a stored object, in the `object` domain
    store = memory_key_store({1: GEN1})
    blob = envelope.encrypt(b"not a key", file_id=FILE_ID, key=GEN1, key_id=1, domain="object")

    # when & then — the domain label stops an object slot from being read as a wrap
    with pytest.raises(AuthenticationError):
        envelope.unwrap_generation(blob, key_id=2, keys=store)


def test_a_key_wrap_is_not_an_object(memory_key_store) -> None:
    # given — a wrap, in the `key-wrap` domain
    store = memory_key_store({1: GEN1})
    blob = envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1)

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.decrypt(blob, file_id=FILE_ID, keys=store, domain="object")


def test_a_wrap_blob_shorter_than_the_minimum_is_rejected(memory_key_store) -> None:
    # given
    store = memory_key_store({1: GEN1})

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.unwrap_generation(bytes(envelope.MIN_BLOB_SIZE - 1), key_id=2, keys=store)


def test_an_unknown_version_is_refused(memory_key_store) -> None:
    # given
    store = memory_key_store({1: GEN1})
    blob = bytearray(envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1))
    blob[0] = 2

    # when & then
    with pytest.raises(UnsupportedEnvelopeVersion):
        envelope.unwrap_generation(bytes(blob), key_id=2, keys=store)


def test_generation_zero_in_the_header_is_rejected(memory_key_store) -> None:
    # given
    store = memory_key_store({1: GEN1})
    blob = bytearray(envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1))
    blob[1:5] = bytes(4)

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.unwrap_generation(bytes(blob), key_id=2, keys=store)


def test_a_wrong_size_key_is_a_caller_error() -> None:
    # when & then
    with pytest.raises(ValueError):
        envelope.wrap_generation(b"short", key_id=2, under=GEN1, under_id=1)


@pytest.mark.parametrize("index", [5, 17, -1], ids=["nonce", "ciphertext", "tag"])
def test_a_flipped_byte_region_is_rejected(index, memory_key_store) -> None:
    # given
    store = memory_key_store({1: GEN1})
    blob = bytearray(envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1))
    blob[index] ^= 0x01

    # when & then
    with pytest.raises(AuthenticationError):
        envelope.unwrap_generation(bytes(blob), key_id=2, keys=store)


def test_nonces_are_unique_across_many_wraps() -> None:
    # when
    nonces = {envelope.wrap_generation(GEN2, key_id=2, under=GEN1, under_id=1)[5:17] for _ in range(64)}

    # then
    assert len(nonces) == 64


# --------------------------------------------------------------------------- the port


def test_the_cipher_can_wrap_the_next_generation(memory_key_store) -> None:
    # given — the ring holds both generations, as a rotating machine does
    store = memory_key_store({1: GEN1, 2: GEN2})
    cipher = EnvelopeCipher(store)

    # when
    blob = cipher.wrap(GEN3, key_id=3, under_id=2)

    # then — a machine that holds generation 2 absorbs generation 3
    other = memory_key_store({1: GEN1, 2: GEN2})
    assert EnvelopeCipher(other).unwrap(blob, key_id=3) == GEN3


def test_the_cipher_wrap_needs_the_wrapper_generation(memory_key_store) -> None:
    # given
    cipher = EnvelopeCipher(memory_key_store({2: GEN2}))

    # when & then
    with pytest.raises(UnknownKeyGeneration):
        cipher.wrap(GEN3, key_id=3, under_id=1)


def test_the_cipher_unwrap_resolves_the_wrapper_from_the_ring(memory_key_store) -> None:
    # given
    cipher = EnvelopeCipher(memory_key_store({1: GEN1}))
    blob = EnvelopeCipher(memory_key_store({1: GEN1, 2: GEN2})).wrap(GEN2, key_id=2, under_id=1)

    # when & then
    assert cipher.unwrap(blob, key_id=2) == GEN2


@given(key=st.integers(min_value=2, max_value=1000))
def test_any_generation_pair_round_trips(key, memory_key_store) -> None:
    # given
    store = memory_key_store({1: GEN1})
    distributed = bytes((key + offset) % 256 for offset in range(KEY_SIZE))

    # when
    blob = envelope.wrap_generation(distributed, key_id=key, under=GEN1, under_id=1)

    # then
    assert envelope.unwrap_generation(blob, key_id=key, keys=store) == distributed
