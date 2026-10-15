# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ring export (``docs/design/05-key-generations.md``).

The production scrypt parameters are expensive (~128 MiB), so most cases inject a cheap
`ScryptParams` explicitly; the production defaults themselves are pinned by a signature check
and one real round-trip, so they cannot be silently weakened.
"""

from __future__ import annotations

import inspect
import unicodedata

import pytest

from syncintosh.core.errors import MasterKeyUnwrapError
from syncintosh.crypto.primitives import be32
from syncintosh.crypto.ring_export import (
    MIN_BLOB_SIZE,
    PRODUCTION_SCRYPT,
    RingEntry,
    ScryptParams,
    _pack_ring,
    _unpack_ring,
    unwrap_ring,
    wrap_ring,
)

CHEAP = ScryptParams(log2_n=4, r=1, p=1)
CHEAP_ONLY = frozenset({CHEAP})
RING = (RingEntry(1, bytes(range(32))), RingEntry(2, bytes(range(32, 64))))


def _cheap_blob(ring=RING, passphrase="correct horse") -> bytes:
    return wrap_ring(ring, passphrase, params=CHEAP, allowed=CHEAP_ONLY)


def _raw_ring(*key_ids: int) -> bytes:
    """Hand-build an export plaintext with the given generation ids, for the parser tests."""
    entries = b"".join(be32(key_id) + bytes(32) for key_id in key_ids)
    return len(key_ids).to_bytes(4, "big") + entries


def test_pack_is_ascending_with_generation_one_first() -> None:
    # when — the input order is not the output order
    packed = _pack_ring((RingEntry(2, bytes(range(32, 64))), RingEntry(1, bytes(range(32)))))

    # then
    assert packed[:4] == (2).to_bytes(4, "big")
    assert packed[4:8] == (1).to_bytes(4, "big")
    assert _unpack_ring(packed) == RING


def test_roundtrip_with_cheap_params() -> None:
    # given
    blob = _cheap_blob()

    # when
    ring = unwrap_ring(blob, "correct horse", allowed=CHEAP_ONLY)

    # then
    assert ring == RING


def test_a_wrong_passphrase_is_a_unwrap_error() -> None:
    # given
    blob = _cheap_blob()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(blob, "wrong horse", allowed=CHEAP_ONLY)


def test_a_damaged_tag_is_a_unwrap_error() -> None:
    # given
    damaged = bytearray(_cheap_blob())
    damaged[-1] ^= 0x01

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(bytes(damaged), "correct horse", allowed=CHEAP_ONLY)


def test_parameters_outside_the_allowlist_are_refused_before_the_kdf() -> None:
    # given — a cheap blob the default (production) allowlist must not run
    blob = _cheap_blob()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(blob, "correct horse")


def test_wrap_refuses_parameters_outside_its_allowlist() -> None:
    # when & then — never write an export this build would refuse to open
    with pytest.raises(ValueError):
        wrap_ring(RING, "correct horse", params=CHEAP)


def test_a_passphrase_is_normalized_to_nfc() -> None:
    # given — the same Korean text in composed (NFC) and decomposed (NFD) forms
    composed = "비밀번호"
    decomposed = unicodedata.normalize("NFD", composed)

    # when — written decomposed, read composed
    blob = wrap_ring(RING, decomposed, params=CHEAP, allowed=CHEAP_ONLY)
    ring = unwrap_ring(blob, composed, allowed=CHEAP_ONLY)

    # then
    assert composed != decomposed
    assert ring == RING


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
        unwrap_ring(bytes(damaged), "correct horse", allowed=CHEAP_ONLY)


def test_a_blob_shorter_than_the_minimum_is_a_unwrap_error() -> None:
    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        unwrap_ring(bytes(MIN_BLOB_SIZE - 1), "correct horse", allowed=CHEAP_ONLY)


def test_production_params_are_the_documented_work_factor() -> None:
    # when & then — changing these is a security decision, not a tweak
    assert ScryptParams(log2_n=17, r=8, p=1) == PRODUCTION_SCRYPT
    assert PRODUCTION_SCRYPT.n == 2**17


def test_the_shipped_defaults_are_the_production_parameters() -> None:
    # when
    params_default = inspect.signature(wrap_ring).parameters["params"].default
    wrap_allowed_default = inspect.signature(wrap_ring).parameters["allowed"].default
    unwrap_allowed_default = inspect.signature(unwrap_ring).parameters["allowed"].default

    # then
    assert params_default == PRODUCTION_SCRYPT
    assert wrap_allowed_default == frozenset({PRODUCTION_SCRYPT})
    assert unwrap_allowed_default == frozenset({PRODUCTION_SCRYPT})


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
        ((RingEntry(2, bytes(32)),), "generation 1"),
        ((RingEntry(1, bytes(32)), RingEntry(1, bytes(32))), "two keys"),
        ((RingEntry(1, bytes(16)),), "32 bytes"),
    ],
    ids=["empty", "no_generation_one", "duplicate_id", "short_key"],
)
def test_an_invalid_ring_is_refused(ring, message) -> None:
    # when & then
    with pytest.raises(ValueError, match=message):
        _pack_ring(ring)


@pytest.mark.parametrize(
    "plaintext",
    [
        b"",
        _raw_ring(),
        (2).to_bytes(4, "big") + bytes(36),
        _raw_ring(2, 1),
        _raw_ring(2),
        _raw_ring(1, 1),
    ],
    ids=["empty", "zero_count", "length_mismatch", "unsorted", "not_from_one", "duplicate"],
)
def test_a_malformed_plaintext_is_a_unwrap_error(plaintext) -> None:
    # when & then — the plaintext is authenticated, so a violation is a bug, not an attack
    with pytest.raises(MasterKeyUnwrapError):
        _unpack_ring(plaintext)


def test_ring_entry_repr_masks_the_key() -> None:
    # given
    secret = bytes(range(32))

    # when
    text = repr(RingEntry(1, secret))

    # then
    assert "key_id=1" in text
    assert "masked" in text
    assert secret.hex() not in text
