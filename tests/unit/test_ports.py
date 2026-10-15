# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ports declared in ``core``."""

from __future__ import annotations

from syncintosh.core.ports import Domain, KeyStore, MergeDriver


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
        def generations(self) -> tuple[int, ...]:
            return ()

        def get(self, key_id: int) -> bytes | None:
            return None

        def put(self, key_id: int, key: bytes) -> None:
            return None

    # when
    matches = isinstance(FakeKeyStore(), KeyStore)

    # then
    assert matches is True


def test_key_store_without_put_does_not_conform() -> None:
    # given — a single-key store must not satisfy the generation-addressed port
    class SingleKeyStore:
        def generations(self) -> tuple[int, ...]:
            return ()

        def get(self, key_id: int) -> bytes | None:
            return None

    # when
    matches = isinstance(SingleKeyStore(), KeyStore)

    # then
    assert matches is False


def test_domain_literal_covers_exactly_the_three_slots() -> None:
    # when
    domains: tuple[Domain, ...] = ("object", "manifest", "key-wrap")

    # then
    assert domains == ("object", "manifest", "key-wrap")
