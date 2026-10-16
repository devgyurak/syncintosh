# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the folded path-collision key.

macOS's default filesystem is case- and normalization-insensitive, so two spellings of one path
must collide (``docs/design/02-base-and-merge.md``).
"""

from __future__ import annotations

import unicodedata

import pytest

from syncintosh.core.paths import collision_key


@pytest.mark.parametrize(
    "pair",
    [
        (".Zshrc", ".zshrc"),
        ("Library/Application Support/Code/User/settings.json", "library/application support/code/user/settings.json"),
        ("caf\u00e9", "cafe\u0301"),  # NFC vs NFD
    ],
)
def test_two_spellings_of_one_path_share_a_key(pair: tuple[str, str]) -> None:
    # when
    first, second = collision_key(pair[0]), collision_key(pair[1])

    # then
    assert first == second


def test_different_paths_keep_different_keys() -> None:
    # when & then
    assert collision_key(".zshrc") != collision_key(".zshenv")


def test_the_key_is_normalized_to_nfc() -> None:
    # when
    key = collision_key("cafe\u0301")

    # then
    assert key == unicodedata.normalize("NFC", "caf\u00e9")
