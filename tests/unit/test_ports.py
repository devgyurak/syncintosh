# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ports declared in ``core``."""

from __future__ import annotations

from syncintosh.core.ports import KeyStore, MergeDriver


def test_merge_driver_port_is_structural() -> None:
    # given
    class FakeDriver:
        def merge(self, base: str, local: str, remote: str) -> str:
            return local

    # when
    matches = isinstance(FakeDriver(), MergeDriver)

    # then
    assert matches is True


def test_key_store_port_is_structural() -> None:
    # given
    class FakeKeyStore:
        def get(self) -> bytes | None:
            return None

        def set(self, key: bytes) -> None:
            return None

    # when
    matches = isinstance(FakeKeyStore(), KeyStore)

    # then
    assert matches is True
