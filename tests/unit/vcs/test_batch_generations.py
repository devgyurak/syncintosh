# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the streamed `cat-file --batch` reader that decides key reachability."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from syncintosh.core.errors import GitError
from syncintosh.vcs.git import _DISCARD_CHUNK, _batch_generations

OID = "a" * 40
OTHER = "b" * 40


def _header(generation: int) -> bytes:
    return bytes([1]) + generation.to_bytes(4, "big")


def _response(oid: str, body: bytes) -> bytes:
    return f"{oid} blob {len(body)}\n".encode("ascii") + body + b"\n"


def _read(responses: bytes, oids: tuple[str, ...]) -> tuple[int, ...]:
    return _batch_generations(io.BytesIO(), io.BytesIO(responses), oids)


@pytest.mark.parametrize(
    ("responses", "oids", "expected"),
    [
        (_response(OID, _header(3) + b"rest"), (OID,), (3,)),
        (f"{OID} missing\n".encode("ascii"), (OID,), ()),
        (_response(OID, b""), (OID,), ()),
        (_response(OID, _header(2)), (OID,), (2,)),
        (_response(OID, _header(4) + b"x" * (_DISCARD_CHUNK * 2 + 17)), (OID,), (4,)),
        (_response(OID, _header(5)) + _response(OTHER, _header(1)), (OID, OTHER), (1, 5)),
        (f"{OID} missing\n".encode("ascii") + _response(OTHER, _header(6)), (OID, OTHER), (6,)),
        (_response(OID, _header(2)) + _response(OTHER, _header(2)), (OID, OTHER), (2,)),
    ],
    ids=[
        "header_and_body",
        "missing",
        "zero_bytes",
        "exactly_five",
        "larger_than_chunk",
        "two",
        "skips_missing",
        "dedup",
    ],
)
def test_the_generations_are_read_from_the_streamed_headers(responses, oids, expected) -> None:
    # when
    generations = _read(responses, oids)

    # then
    assert generations == expected


def test_each_oid_is_requested_one_at_a_time() -> None:
    # given
    requests = io.BytesIO()

    # when
    _batch_generations(requests, io.BytesIO(_response(OID, _header(1)) + _response(OTHER, _header(1))), (OID, OTHER))

    # then
    assert requests.getvalue() == f"{OID}\n{OTHER}\n".encode("ascii")


@pytest.mark.parametrize(
    "responses",
    [
        b"",
        b"garbage\n",
        f"{OID} blob notanumber\n".encode("ascii"),
        f"{OTHER} blob 3\nabc\n".encode("ascii"),
        f"{OID} blob 50\n".encode("ascii") + _header(1),
        f"{OID} blob {_DISCARD_CHUNK * 2}\n".encode("ascii") + _header(1) + b"short",
    ],
    ids=["eof", "no_fields", "bad_size", "wrong_oid", "truncated_head_body", "truncated_discard"],
)
def test_a_malformed_or_cut_off_stream_is_a_git_error(responses) -> None:
    # when & then
    with pytest.raises(GitError):
        _read(responses, (OID,))


def test_importing_the_git_transport_does_not_import_cryptography() -> None:
    # given — a fresh interpreter, so modules other tests imported are absent
    code = "import sys, syncintosh.vcs.git; print('cryptography' in sys.modules)"

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then — the header parse is core's, so vcs no longer reaches into crypto
    assert result.stdout.strip() == "False"
