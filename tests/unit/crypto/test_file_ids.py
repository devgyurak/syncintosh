# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the crypto file ID source."""

from __future__ import annotations

from syncintosh.core.ids import is_file_id
from syncintosh.crypto.file_ids import SecureFileIdSource


def test_new_file_id_is_32_lowercase_hex() -> None:
    # given
    source = SecureFileIdSource()

    # when
    file_id = source.new_file_id()

    # then
    assert len(file_id) == 32
    assert is_file_id(file_id) is True


def test_successive_ids_are_distinct() -> None:
    # given
    source = SecureFileIdSource()

    # when
    ids = {source.new_file_id() for _ in range(8)}

    # then
    assert len(ids) == 8
