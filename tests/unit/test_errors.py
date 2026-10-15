# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the crypto error hierarchy declared in ``core``."""

from __future__ import annotations

from syncintosh.core.errors import (
    AuthenticationError,
    CryptoError,
    MasterKeyUnwrapError,
    UnknownKeyGeneration,
    UnsupportedEnvelopeVersion,
)


def test_every_crypto_error_derives_from_the_base() -> None:
    # given
    kinds = (AuthenticationError, UnsupportedEnvelopeVersion, UnknownKeyGeneration, MasterKeyUnwrapError)

    # when
    derived = [issubclass(kind, CryptoError) for kind in kinds]

    # then
    assert derived == [True, True, True, True]


def test_unknown_generation_is_distinct_from_authentication_failure() -> None:
    # given — "import the key" must never look like "the file is corrupt"
    error = UnknownKeyGeneration(3)

    # when & then
    assert error.key_id == 3
    assert isinstance(error, CryptoError)
    assert not isinstance(error, AuthenticationError)
    assert "3" in str(error)
