# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the in-memory `KeyStore` fake used across the suite.

The fake must mirror the real store's contract — in particular the hard error on a second,
different key for one generation id — or a later import/rotation test could rely on a silent
overwrite the real store refuses.
"""

from __future__ import annotations

import pytest

KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))


def test_put_then_get_returns_the_key(memory_key_store) -> None:
    # given
    store = memory_key_store()

    # when
    store.put(1, KEY)

    # then
    assert store.get(1) == KEY
    assert store.generations() == (1,)


def test_a_missing_generation_is_none(memory_key_store) -> None:
    # given
    store = memory_key_store()

    # when & then
    assert store.get(1) is None


def test_a_different_key_for_one_generation_is_refused(memory_key_store) -> None:
    # given — the real store treats this as a hard error, so the fake must too
    store = memory_key_store({1: KEY})

    # when & then
    with pytest.raises(ValueError):
        store.put(1, OTHER_KEY)


def test_putting_the_same_key_again_is_allowed(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY})

    # when
    store.put(1, KEY)

    # then
    assert store.get(1) == KEY
