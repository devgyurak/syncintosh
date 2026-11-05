# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the key-free envelope header parse (``docs/design/04-envelope-v1.md``)."""

from __future__ import annotations

import pytest

from syncintosh.core.envelope_header import ENVELOPE_VERSION, HEADER_SIZE, KEY_ID_SIZE, header_generation


def test_the_header_is_a_version_byte_then_a_four_byte_key_id() -> None:
    # when & then
    assert (ENVELOPE_VERSION, KEY_ID_SIZE, HEADER_SIZE) == (1, 4, 5)


@pytest.mark.parametrize("key_id", [1, 7, 0xFFFF_FFFF])
def test_header_generation_reads_the_key_id_without_a_key(key_id) -> None:
    # given
    blob = bytes([ENVELOPE_VERSION]) + key_id.to_bytes(KEY_ID_SIZE, "big") + b"nonce-and-ciphertext"

    # when
    generation = header_generation(blob)

    # then
    assert generation == key_id


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x01\x00\x00",
        bytes([2]) + (1).to_bytes(4, "big"),
        bytes([1]) + bytes(4),
        b"not an envelope at all",
    ],
    ids=["empty", "short", "unknown_version", "generation_zero", "junk"],
)
def test_header_generation_returns_none_for_a_non_envelope(blob) -> None:
    # when & then
    assert header_generation(blob) is None
