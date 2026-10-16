# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the keyed commitment.

The commitment must be a pure function of the generation key and the public domain string, and
must never accept a wrong-size key as if it were one.
"""

from __future__ import annotations

import hashlib
import hmac
import re

import pytest

from syncintosh.crypto.commitments import COMMITMENT_DOMAIN, key_commitment

KEY = bytes(range(32))


def test_the_commitment_is_a_lowercase_hex_sha256_mac() -> None:
    # when
    commitment = key_commitment(KEY)

    # then
    assert re.fullmatch(r"[0-9a-f]{64}", commitment) is not None
    assert commitment == hmac.new(KEY, COMMITMENT_DOMAIN, hashlib.sha256).hexdigest()


def test_the_domain_string_is_pinned() -> None:
    # when & then — the versioned label is a contract, so a change is a deliberate migration
    assert COMMITMENT_DOMAIN == b"syncintosh/v1/commit"


def test_the_commitment_is_deterministic() -> None:
    # when & then
    assert key_commitment(KEY) == key_commitment(KEY)


def test_different_keys_commit_differently() -> None:
    # when & then
    assert key_commitment(b"\x00" * 32) != key_commitment(b"\x01" * 32)


def test_a_wrong_size_key_is_a_caller_error() -> None:
    # when & then
    with pytest.raises(ValueError):
        key_commitment(b"too short")
