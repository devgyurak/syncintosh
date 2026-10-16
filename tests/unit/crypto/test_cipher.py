# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `Cipher` implementation over the envelope and a key ring."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import AuthenticationError, UnknownKeyGeneration
from syncintosh.core.ports import Cipher
from syncintosh.crypto.cipher import EnvelopeCipher

KEY = bytes(range(32))
FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
OTHER_FILE_ID = "f" * 32


def test_seal_then_open_round_trips(memory_key_store) -> None:
    # given
    cipher = EnvelopeCipher(memory_key_store({1: KEY}))

    # when
    blob = cipher.seal(b"hello", domain="object", file_id=FILE_ID, key_id=1)

    # then
    assert cipher.open(blob, domain="object", file_id=FILE_ID) == b"hello"


def test_the_manifest_domain_uses_the_empty_subject(memory_key_store) -> None:
    # given
    cipher = EnvelopeCipher(memory_key_store({1: KEY}))

    # when
    blob = cipher.seal(b'{"schema":1}', domain="manifest", file_id="", key_id=1)

    # then
    assert cipher.open(blob, domain="manifest", file_id="") == b'{"schema":1}'


def test_sealing_under_an_absent_generation_is_unknown(memory_key_store) -> None:
    # given
    cipher = EnvelopeCipher(memory_key_store({}))

    # when & then
    with pytest.raises(UnknownKeyGeneration):
        cipher.seal(b"x", domain="object", file_id=FILE_ID, key_id=1)


def test_opening_under_another_file_id_fails(memory_key_store) -> None:
    # given — the ID is bound into the AAD, so a slot move must not authenticate
    cipher = EnvelopeCipher(memory_key_store({1: KEY}))
    blob = cipher.seal(b"x", domain="object", file_id=FILE_ID, key_id=1)

    # when & then
    with pytest.raises(AuthenticationError):
        cipher.open(blob, domain="object", file_id=OTHER_FILE_ID)


def test_the_cipher_satisfies_the_port(memory_key_store) -> None:
    # when
    cipher = EnvelopeCipher(memory_key_store({1: KEY}))

    # then
    assert isinstance(cipher, Cipher)
