# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `RingCodec` implementation over the ring export and the commitment.

Cheap scrypt parameters are passed explicitly; the production default is never reduced
(``AGENTS.md`` §3.1).
"""

from __future__ import annotations

import pytest

from syncintosh.core.errors import MasterKeyUnwrapError
from syncintosh.core.ports import GENERATION_KEY_SIZE, Ring
from syncintosh.crypto.commitments import key_commitment
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.primitives import KEY_SIZE
from syncintosh.crypto.ring_export import ScryptParams

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
KEY_ONE = b"\x01" * 32
KEY_TWO = b"\x02" * 32
RING: Ring = ((1, KEY_ONE), (2, KEY_TWO))
PASSPHRASE = "a recovery passphrase"


def _codec() -> KeyMaterial:
    return KeyMaterial(params=CHEAP, allowed=ALLOWED)


def test_a_ring_round_trips_through_wrap_and_unwrap() -> None:
    # given
    blob = _codec().wrap(RING, PASSPHRASE)

    # when
    ring = _codec().unwrap(blob, PASSPHRASE)

    # then
    assert ring == RING


def test_a_wrong_passphrase_is_one_error() -> None:
    # given
    blob = _codec().wrap(RING, PASSPHRASE)

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        _codec().unwrap(blob, "the wrong passphrase")


def test_unwrap_refuses_parameters_outside_the_allowlist() -> None:
    # given — the blob is cheap, but this build is configured to run only other parameters
    blob = _codec().wrap(RING, PASSPHRASE)
    restrictive = KeyMaterial(params=CHEAP, allowed=frozenset())

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        restrictive.unwrap(blob, PASSPHRASE)


def test_wrap_refuses_parameters_it_would_not_unwrap() -> None:
    # given — an export this build cannot open must never be written
    restrictive = KeyMaterial(params=CHEAP, allowed=frozenset())

    # when & then
    with pytest.raises(ValueError, match="will not unwrap"):
        restrictive.wrap(RING, PASSPHRASE)


def test_commitment_delegates_to_the_keyed_prf() -> None:
    # when
    commitment = _codec().commitment(KEY_ONE)

    # then
    assert commitment == key_commitment(KEY_ONE)


def test_the_generation_key_size_matches_the_crypto_layer() -> None:
    # when & then — `core` mints the key, so the two owners of that width must agree
    assert GENERATION_KEY_SIZE == KEY_SIZE
