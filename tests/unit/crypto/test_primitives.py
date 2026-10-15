# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the shared crypto framing primitives."""

from __future__ import annotations

import pytest

from syncintosh.crypto.primitives import be32, length_prefixed


def test_be32_is_big_endian() -> None:
    # when & then
    assert be32(0) == b"\x00\x00\x00\x00"
    assert be32(1) == b"\x00\x00\x00\x01"
    assert be32(0x01020304) == b"\x01\x02\x03\x04"
    assert be32(0xFFFF_FFFF) == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("value", [-1, 2**32], ids=["negative", "too_large"])
def test_be32_rejects_out_of_range_values(value) -> None:
    # when & then
    with pytest.raises(ValueError):
        be32(value)


def test_length_prefixed_pins_every_field_boundary() -> None:
    # when — an empty field still gets its own zero-length prefix
    framed = length_prefixed(b"ab", b"", b"cde")

    # then
    assert framed == b"\x00\x00\x00\x02ab" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x03cde"
