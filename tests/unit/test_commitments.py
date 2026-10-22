# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the commitment map's shape and canonical serialization.

The no-rewrite rule compares plaintext bytes, so the same map must serialize identically
whatever order it was built in (``docs/design/03-manifest.md``).
"""

from __future__ import annotations

import pytest

from syncintosh.core.commitments import dumps_commitments, loads_commitments
from syncintosh.core.errors import CommitmentsCorrupt

HEX_A = "a" * 64
HEX_B = "b" * 64


def test_a_map_round_trips() -> None:
    # given
    commitments = {1: HEX_A, 2: HEX_B}

    # when
    parsed = loads_commitments(dumps_commitments(commitments))

    # then
    assert parsed == commitments


def test_serialization_is_independent_of_insertion_order() -> None:
    # when
    first = dumps_commitments({2: HEX_B, 1: HEX_A})
    second = dumps_commitments({1: HEX_A, 2: HEX_B})

    # then
    assert first == second
    assert first == ('{"1":"' + HEX_A + '","2":"' + HEX_B + '"}').encode()


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'"scalar"',
        b"not json",
        b'{"0":"' + HEX_A.encode() + b'"}',
        b'{"01":"' + HEX_A.encode() + b'"}',
        b'{"1":"short"}',
        b'{"1":7}',
        b'{"1":"' + HEX_A.encode() + b'","1":"' + HEX_B.encode() + b'"}',
        b'{"4294967296":"' + HEX_A.encode() + b'"}',
        b'{"' + b"9" * 5000 + b'":"' + HEX_A.encode() + b'"}',
        b"\xff\xfe",
    ],
)
def test_a_corrupt_map_is_refused(payload: bytes) -> None:
    # when & then
    with pytest.raises(CommitmentsCorrupt):
        loads_commitments(payload)


@pytest.mark.parametrize(
    "commitments",
    [
        {0: HEX_A},
        {1: "short"},
        {1: 7},
        {2**32: HEX_A},
    ],
)
def test_the_encoder_refuses_what_the_parser_would_refuse(commitments) -> None:
    # when & then
    with pytest.raises(CommitmentsCorrupt):
        dumps_commitments(commitments)
