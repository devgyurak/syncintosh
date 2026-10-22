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
        self.deletes: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        return self._entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._entries[(service, username)] = password
        self.writes.append(username)

    def delete_password(self, service: str, username: str) -> None:
        self._entries.pop((service, username), None)
        self.deletes.append(username)


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
    with pytest.raises(KeyStoreCorrupt):
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
    # given — the index lists generation 1, but its entry is not hex
    backend = FakeBackend()
    backend.set_password(NAMESPACE, INDEX_USERNAME, json.dumps([1]))
    backend.set_password(NAMESPACE, "gen-1", "not hex")
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then — a CryptoError, so a caller branching on it does not crash
    with pytest.raises(KeyStoreCorrupt):
        store.get(1)


def test_a_wrong_length_entry_is_a_crypto_error() -> None:
    # given — the index lists generation 1, but its entry is the wrong width
    backend = FakeBackend()
    backend.set_password(NAMESPACE, INDEX_USERNAME, json.dumps([1]))
    backend.set_password(NAMESPACE, "gen-1", bytes(16).hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then
    with pytest.raises(KeyStoreCorrupt):
        store.get(1)


def test_a_key_the_index_does_not_list_is_invisible() -> None:
    # given — a crash between `put`'s two writes left a key entry with no index entry
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-2", OTHER_KEY.hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then — the index is the only source of truth, so the orphan is not held
    assert store.generations() == ()
    assert store.get(2) is None


def test_put_repairs_a_key_the_index_does_not_list() -> None:
    # given — an orphan entry for generation 2
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-2", OTHER_KEY.hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when — a distributed generation with that id arrives
    store.put(2, KEY)

    # then — the orphan is overwritten instead of colliding, and is now held
    assert store.get(2) == KEY
    assert store.generations() == (2,)


def test_the_master_generation_is_visible_without_its_index_entry() -> None:
    # given — the keychain lost the index entry but kept the master key
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-1", KEY.hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then — gen 1 is never hidden, so `init` refuses instead of clobbering it
    assert store.generations() == ()
    assert store.get(1) == KEY


def test_put_of_the_master_generation_repairs_a_lost_index_entry() -> None:
    # given — an index-less master key
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-1", KEY.hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when — the same key is imported again (`init --import`, or a recovery)
    store.put(1, KEY)

    # then — the index is repaired and the key is unchanged
    assert store.generations() == (1,)
    assert store.get(1) == KEY


def test_put_of_the_master_generation_refuses_a_different_key() -> None:
    # given — an index-less master key
    backend = FakeBackend()
    backend.set_password(NAMESPACE, "gen-1", KEY.hex())
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then — a fresh `init` minting a new gen 1 must not clobber the live master key
    with pytest.raises(ValueError):
        store.put(1, OTHER_KEY)
    assert store.get(1) == KEY


def test_put_on_a_corrupt_entry_surfaces_the_corruption() -> None:
    # given — the index lists generation 1, whose entry is not hex
    backend = FakeBackend()
    backend.set_password(NAMESPACE, INDEX_USERNAME, json.dumps([1]))
    backend.set_password(NAMESPACE, "gen-1", "not hex")
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then — a held-but-corrupt entry is not silently overwritten
    with pytest.raises(KeyStoreCorrupt):
        store.put(1, KEY)


@pytest.mark.parametrize(
    "raw",
    ["12", "5", "[0, -3]", "[1, 2.5]", "[true]", "[1, 1]", f"[{2**32}]"],
    ids=["string_number", "bare_number", "non_positive", "float", "bool", "duplicate", "beyond_uint32"],
)
def test_an_index_that_is_not_a_list_of_ids_is_a_loud_failure(raw) -> None:
    # given
    backend = FakeBackend()
    backend.set_password(NAMESPACE, INDEX_USERNAME, raw)
    store = KeyringKeyStore(NAMESPACE, backend)

    # when & then
    with pytest.raises(KeyStoreCorrupt):
        store.generations()


def test_drop_removes_the_key_and_its_index_entry() -> None:
    # given
    backend = FakeBackend()
    store = KeyringKeyStore(NAMESPACE, backend)
    store.put(1, KEY)
    store.put(2, OTHER_KEY)

    # when
    store.drop(2)

    # then
    assert store.generations() == (1,)
    assert store.get(2) is None
    assert "gen-2" in backend.deletes
    assert json.loads(backend.get_password(NAMESPACE, INDEX_USERNAME) or "[]") == [1]


def test_dropping_an_absent_generation_is_a_no_op() -> None:
    # given — a discard must be idempotent, so a retry after a crash does not fail
    backend = FakeBackend()
    store = KeyringKeyStore(NAMESPACE, backend)
    store.put(1, KEY)
    before = list(backend.deletes)

    # when
    store.drop(9)

    # then
    assert backend.deletes == before
    assert store.generations() == (1,)


def test_the_master_generation_cannot_be_dropped() -> None:
    # given — a ring without generation 1 could not be exported for recovery
    store = KeyringKeyStore(NAMESPACE, FakeBackend())
    store.put(1, KEY)

    # when & then
    with pytest.raises(ValueError):
        store.drop(1)
    assert store.get(1) == KEY


class _FailingDeleteBackend(FakeBackend):
    """A backend whose key-entry delete fails, as a locked keychain can."""

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("the keychain refused the delete")


def test_drop_tolerates_an_index_entry_whose_key_is_already_gone() -> None:
    # given — a crash left the index listing generation 2 while its key entry is gone
    backend = FakeBackend()
    store = KeyringKeyStore(NAMESPACE, backend)
    store.put(1, KEY)
    backend.set_password(NAMESPACE, INDEX_USERNAME, json.dumps([1, 2]))

    # when
    store.drop(2)

    # then — the stale index entry is removed, and deleting a missing key is not attempted
    assert store.generations() == (1,)
    assert "gen-2" not in backend.deletes


def test_drop_updates_the_index_before_removing_the_key() -> None:
    # given — the key-entry delete fails
    store = KeyringKeyStore(NAMESPACE, _FailingDeleteBackend())
    store.put(1, KEY)
    store.put(2, OTHER_KEY)

    # when & then — the failure surfaces, but the index is already consistent
    with pytest.raises(RuntimeError):
        store.drop(2)
    assert store.generations() == (1,)
