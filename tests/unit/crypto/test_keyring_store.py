# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the keyring-backed `KeyStore`.

Every case uses an in-memory fake backend — the real keychain is never touched
(``AGENTS.md`` §5.6). The lazy-backend case patches `keyring.get_keyring`, which the store
only calls on its first method invocation.
"""

from __future__ import annotations

import json

import pytest

from syncintosh.core.errors import KeyStoreCorrupt
from syncintosh.core.ports import KeyStore
from syncintosh.crypto.keyring_store import INDEX_USERNAME, KeyringKeyStore

NAMESPACE = "syncintosh:test"
KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))


class FakeBackend:
    """A minimal in-memory keyring backend that records writes."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], str] = {}
        self.writes: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        return self._entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._entries[(service, username)] = password
        self.writes.append(username)


def test_the_store_conforms_to_the_key_store_port() -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())

    # when
    matches = isinstance(store, KeyStore)

    # then
    assert matches is True


def test_an_empty_store_has_no_generations() -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())

    # when & then
    assert store.generations() == ()
    assert store.get(1) is None


def test_put_then_get_returns_the_key() -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())

    # when
    store.put(1, KEY)

    # then
    assert store.get(1) == KEY
    assert store.generations() == (1,)


def test_generations_are_ascending_whatever_the_insertion_order() -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())

    # when
    store.put(3, KEY)
    store.put(1, KEY)
    store.put(2, OTHER_KEY)

    # then
    assert store.generations() == (1, 2, 3)


def test_putting_the_same_key_again_is_a_no_op() -> None:
    # given
    backend = FakeBackend()
    store = KeyringKeyStore(NAMESPACE, backend)
    store.put(1, KEY)
    before = list(backend.writes)

    # when
    store.put(1, KEY)

    # then
    assert backend.writes == before
    assert store.generations() == (1,)


def test_putting_a_different_key_for_one_generation_is_refused() -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())
    store.put(1, KEY)

    # when & then
    with pytest.raises(ValueError):
        store.put(1, OTHER_KEY)


@pytest.mark.parametrize("key_id", [0, -1])
def test_a_non_positive_generation_is_refused(key_id) -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())

    # when & then
    with pytest.raises(ValueError):
        store.put(key_id, KEY)


def test_a_wrong_size_key_is_refused() -> None:
    # given
    store = KeyringKeyStore(NAMESPACE, FakeBackend())

    # when & then
    with pytest.raises(ValueError):
        store.put(1, b"short")


def test_keys_and_the_index_live_in_their_own_namespaces() -> None:
    # given
    backend = FakeBackend()
    a = KeyringKeyStore("machine-a", backend)
    b = KeyringKeyStore("machine-b", backend)

    # when
    a.put(1, KEY)

    # then
    assert a.get(1) == KEY
    assert b.get(1) is None
    assert b.generations() == ()


def test_a_corrupt_index_is_a_loud_failure() -> None:
    # given
    backend = FakeBackend()
    backend.set_password(NAMESPACE, INDEX_USERNAME, "{not json")
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then
    with pytest.raises(ValueError):
        store.generations()


def test_a_key_id_is_stored_as_lowercase_hex() -> None:
    # given
    backend = FakeBackend()
    store = KeyringKeyStore(NAMESPACE, backend)

    # when
    store.put(2, KEY)

    # then
    assert backend.get_password(NAMESPACE, "gen-2") == KEY.hex()
    assert json.loads(backend.get_password(NAMESPACE, INDEX_USERNAME) or "[]") == [2]


def test_the_backend_is_resolved_lazily_on_the_first_call(monkeypatch) -> None:
    # given — resolving the real backend would raise, so construction must not resolve it
    import keyring

    def _boom():
        raise RuntimeError("backend resolved")

    monkeypatch.setattr(keyring, "get_keyring", _boom)
    store = KeyringKeyStore(NAMESPACE)

    # when & then
    with pytest.raises(RuntimeError):
        store.generations()


def test_a_non_hex_entry_is_a_crypto_error() -> None:
    # given
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-1", "not hex")
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then — a CryptoError, so a caller branching on it does not crash
    with pytest.raises(KeyStoreCorrupt):
        store.get(1)


def test_a_wrong_length_entry_is_a_crypto_error() -> None:
    # given
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-1", bytes(16).hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then
    with pytest.raises(KeyStoreCorrupt):
        store.get(1)


@pytest.mark.parametrize(
    "raw",
    ["12", "5", "[0, -3]", "[1, 2.5]", "[true]"],
    ids=["string_number", "bare_number", "non_positive", "float", "bool"],
)
def test_an_index_that_is_not_a_list_of_ids_is_a_loud_failure(raw) -> None:
    # given
    backend = FakeBackend()
    backend.set_password(NAMESPACE, INDEX_USERNAME, raw)
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then
    with pytest.raises(ValueError):
        store.generations()
