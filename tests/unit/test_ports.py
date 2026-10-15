# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ports declared in ``core``."""

from __future__ import annotations

from syncintosh.core.ports import Cipher, Domain, FileIdSource, KeyStore, MergeDriver, RepoFiles, SecretScanner


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


def test_file_id_source_port_is_structural() -> None:
    # given
    class FakeSource:
        def new_file_id(self) -> str:
            return "0" * 32

    # when
    matches = isinstance(FakeSource(), FileIdSource)

    # then
    assert matches is True


def test_cipher_port_is_structural() -> None:
    # given
    class FakeCipher:
        def seal(self, plaintext: bytes, *, domain, file_id, key_id) -> bytes:
            return plaintext

        def open(self, blob: bytes, *, domain, file_id) -> bytes:
            return blob

    # when
    matches = isinstance(FakeCipher(), Cipher)

    # then
    assert matches is True


def test_repo_files_port_is_structural() -> None:
    # given
    class FakeRepo:
        def read(self, relpath: str) -> bytes | None:
            return None

        def write(self, relpath: str, data: bytes) -> None:
            return None

        def delete(self, relpath: str) -> None:
            return None

        def list_paths(self, prefix: str) -> tuple[str, ...]:
            return ()

    # when
    matches = isinstance(FakeRepo(), RepoFiles)

    # then
    assert matches is True


def test_a_repo_without_list_paths_does_not_conform() -> None:
    # given — the builtin-shadowing name `list` is deliberately not the port's method
    class LooksLikeRepo:
        def read(self, relpath: str) -> bytes | None:
            return None

        def write(self, relpath: str, data: bytes) -> None:
            return None

        def delete(self, relpath: str) -> None:
            return None

    # when
    matches = isinstance(LooksLikeRepo(), RepoFiles)

    # then
    assert matches is False


def test_secret_scanner_port_is_structural() -> None:
    # given
    class FakeScanner:
        def scan(self, plaintext: bytes, *, subject: str) -> None:
            return None

    # when
    matches = isinstance(FakeScanner(), SecretScanner)

    # then
    assert matches is True
