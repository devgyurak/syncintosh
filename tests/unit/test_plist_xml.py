# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the canonical property-list form (`core/plist_xml.py`)."""

from __future__ import annotations

import plistlib
from datetime import datetime

import pytest

from syncintosh.core.plist_xml import (
    BINARY_REFUSED,
    MALFORMED_REFUSED,
    ROOT_REFUSED,
    SUBSET_REFUSED,
    canonical_plist,
    dump_plist,
    parse_plist,
)

_HEADER = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
)


def _plist(body: bytes) -> bytes:
    return _HEADER + b'<plist version="1.0">' + body + b"</plist>"


def test_the_canonical_form_sorts_keys_and_is_stable() -> None:
    # given
    unsorted = _plist(b"<dict><key>b</key><integer>2</integer><key>a</key><real>1.0</real></dict>")

    # when
    once = canonical_plist(unsorted)
    twice = canonical_plist(once)

    # then
    assert once.index(b"<key>a</key>") < once.index(b"<key>b</key>")
    assert twice == once


def test_an_integer_and_a_real_keep_their_tags() -> None:
    # given
    source = _plist(b"<dict><key>i</key><integer>1</integer><key>r</key><real>1.0</real></dict>")

    # when
    canonical = canonical_plist(source)

    # then
    assert (b"<integer>1</integer>" in canonical, b"<real>1.0</real>" in canonical) == (True, True)


def test_data_and_date_survive_the_canonical_form() -> None:
    # given
    value = {"d": b"\x01\x02", "t": datetime(2024, 1, 2, 3, 4, 5)}

    # when
    parsed = parse_plist(canonical_plist(dump_plist(value)))

    # then
    assert parsed == value


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY), BINARY_REFUSED),
        (plistlib.dumps(["a"], fmt=plistlib.FMT_XML), ROOT_REFUSED),
        (
            b'<?xml version="1.0"?><!DOCTYPE plist [<!ENTITY x "y">]><plist version="1.0"><dict/></plist>',
            SUBSET_REFUSED,
        ),
        (b"<plist><dict>", MALFORMED_REFUSED),
        (b"", MALFORMED_REFUSED),
    ],
    ids=["binary", "array-root", "internal-subset", "truncated", "empty"],
)
def test_input_it_cannot_write_back_is_refused(data: bytes, reason: str) -> None:
    # when & then
    with pytest.raises(ValueError, match=reason):
        canonical_plist(data)


def test_a_value_plist_cannot_hold_is_refused() -> None:
    # when & then
    with pytest.raises(ValueError, match=MALFORMED_REFUSED):
        dump_plist({"a": object()})
