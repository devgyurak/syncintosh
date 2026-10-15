# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for per-generation, per-domain subkeys (``docs/design/04-envelope-v1.md``)."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import AuthenticationError
from syncintosh.crypto.envelope import decrypt, encrypt
from syncintosh.crypto.hkdf import derive_subkey, hkdf_info

KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))
FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def test_hkdf_info_is_domain_specific() -> None:
    # when
    infos = {hkdf_info("object"), hkdf_info("manifest"), hkdf_info("key-wrap")}

    # then
    assert len(infos) == 3
    assert hkdf_info("object") == b"syncintosh/v1/object"


def test_subkeys_differ_per_domain_generation_and_key() -> None:
    # when
    by_domain = {derive_subkey(KEY, 1, domain) for domain in ("object", "manifest", "key-wrap")}
    by_generation = {derive_subkey(KEY, key_id, "object") for key_id in (1, 2, 3)}
    by_key = {derive_subkey(key, 1, "object") for key in (KEY, OTHER_KEY)}

    # then
    assert len(by_domain) == 3
    assert len(by_generation) == 3
    assert len(by_key) == 2


def test_a_subkey_is_32_bytes() -> None:
    # when & then
    assert len(derive_subkey(KEY, 1, "object")) == 32


def test_a_non_positive_generation_is_a_caller_error() -> None:
    # when & then
    with pytest.raises(ValueError):
        derive_subkey(KEY, 0, "object")


def test_a_wrong_size_generation_key_is_a_caller_error() -> None:
    # when & then
    with pytest.raises(ValueError):
        derive_subkey(b"short", 1, "object")


def test_an_object_key_cannot_open_a_manifest_blob(memory_key_store) -> None:
    # given — one generation, two domains
    store = memory_key_store({1: KEY})
    manifest_blob = encrypt(b'{"schema":1}', file_id="", key=KEY, key_id=1, domain="manifest")

    # when & then
    with pytest.raises(AuthenticationError):
        decrypt(manifest_blob, file_id=FILE_ID, keys=store, domain="object")
