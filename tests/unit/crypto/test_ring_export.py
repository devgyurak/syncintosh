# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ring export (``docs/design/05-key-generations.md``).

The production scrypt parameters are expensive (~128 MiB), so most cases inject a cheap
`ScryptParams` explicitly; the production defaults themselves are pinned by a signature check
and one real round-trip, so they cannot be silently weakened.
"""

from __future__ import annotations

import inspect

import pytest

from syncintosh.core.errors import MasterKeyUnwrapError
from syncintosh.crypto.ring_export import (
    MIN_BLOB_SIZE,
    PRODUCTION_SCRYPT,
    ScryptParams,
    _pack_ring,
    _unpack_ring,
    unwrap_ring,
    wrap_ring,
)

CHEAP = ScryptParams(log2_n=4, r=1, p=1)
RING = ((1, bytes(range(32))), (2, bytes(range(32, 64))))


def _cheap_blob(ring=RING, passphrase="correct horse") -> bytes:
    return wrap_ring(ring, passphrase, params=CHEAP)


def test_pack_is_ascending_with_generation_one_first() -> None:
    # when
    packed = _pack_ring(((2, bytes(range(32, 64))), (1, bytes(range(32)))))

    # then
    assert packed[:4] == (2).to_bytes(4, "big")
    assert packed[4:8] == (1).to_bytes(4, "big")
    assert _unpack_ring(packed) == RING


def test_roundtrip_with_cheap_params() -> None:
    # given
    blob = _cheap_blob()

    # when
    ring = unwrap_ring(blob, "correct horse", allowed=frozenset({CHEAP}))

    # then
    assert ring == RING


def test_a_wrong_passphrase_is_a_unwrap_error() -> None:
    # given
    blob = _cheap_blob()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(blob, "wrong horse", allowed=frozenset({CHEAP}))


def test_a_damaged_tag_is_a_unwrap_error() -> None:
    # given
    damaged = bytearray(_cheap_blob())
    damaged[-1] ^= 0x01

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(bytes(damaged), "correct horse", allowed=frozenset({CHEAP}))


def test_parameters_outside_the_allowlist_are_refused_before_the_kdf() -> None:
    # given — a cheap blob the default (production) allowlist must not run
    blob = _cheap_blob()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(blob, "correct horse")


@pytest.mark.parametrize(
    ("index", "value"),
    [(0, 0x00), (4, 0x02), (5, 0x02), (6, 0x00)],
    ids=["magic", "format", "kdf", "log2_n"],
)
def test_a_damaged_header_is_a_unwrap_error(index, value) -> None:
    # given
    damaged = bytearray(_cheap_blob())
    damaged[index] = value

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(bytes(damaged), "correct horse", allowed=frozenset({CHEAP}))


def test_a_blob_shorter_than_the_minimum_is_a_unwrap_error() -> None:
    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(bytes(MIN_BLOB_SIZE - 1), "correct horse", allowed=frozenset({CHEAP}))


def test_production_params_are_the_documented_work_factor() -> None:
    # when & then — changing these is a security decision, not a tweak
    assert ScryptParams(log2_n=17, r=8, p=1) == PRODUCTION_SCRYPT
    assert PRODUCTION_SCRYPT.n == 2**17


def test_the_shipped_defaults_are_the_production_parameters() -> None:
    # when
    params_default = inspect.signature(wrap_ring).parameters["params"].default
    allowed_default = inspect.signature(unwrap_ring).parameters["allowed"].default

    # then
    assert params_default == PRODUCTION_SCRYPT
    assert allowed_default == frozenset({PRODUCTION_SCRYPT})


def test_production_parameters_really_roundtrip() -> None:
    # given — the real 128 MiB derivation, in the default suite so CI runs it too
    blob = wrap_ring(RING, "a real passphrase", params=PRODUCTION_SCRYPT)

    # when
    ring = unwrap_ring(blob, "a real passphrase")

    # then
    assert ring == RING


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"log2_n": 0, "r": 1, "p": 1}, "log2_n"),
        ({"log2_n": 256, "r": 1, "p": 1}, "log2_n"),
        ({"log2_n": 1, "r": 0, "p": 1}, "r"),
        ({"log2_n": 1, "r": 1, "p": 0}, "p"),
    ],
    ids=["log2_n_low", "log2_n_high", "r", "p"],
)
def test_scrypt_params_reject_bad_fields(kwargs, message) -> None:
    # when & then
    with pytest.raises(ValueError, match=message):
        ScryptParams(**kwargs)


@pytest.mark.parametrize(
    ("ring", "message"),
    [
        ((), "generation 1"),
        (((2, bytes(32)),), "generation 1"),
        (((1, bytes(32)), (1, bytes(32))), "two keys"),
        (((1, bytes(16)),), "32 bytes"),
    ],
    ids=["empty", "no_generation_one", "duplicate_id", "short_key"],
)
def test_an_invalid_ring_is_refused(ring, message) -> None:
    # when & then
    with pytest.raises(ValueError, match=message):
        _pack_ring(ring)


@pytest.mark.parametrize(
    "plaintext",
    [b"", (0).to_bytes(4, "big"), (2).to_bytes(4, "big") + bytes(36)],
    ids=["empty", "zero_count", "length_mismatch"],
)
def test_a_malformed_plaintext_is_a_unwrap_error(plaintext) -> None:
    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        _unpack_ring(plaintext)
