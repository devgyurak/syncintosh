# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `KeyStore` backed by the platform keyring.

The keyring has no enumeration API, so a single index entry lists the generation ids this
machine holds. No I/O happens in the constructor (``AGENTS.md`` §2.6): the backend is resolved
on the first call, so building the store never touches the keychain.
"""

from __future__ import annotations

import json
from typing import Protocol

from syncintosh.core.errors import KeyStoreCorrupt
from syncintosh.crypto.primitives import KEY_SIZE, UINT32_MAX

INDEX_USERNAME = "generations"


class _Backend(Protocol):
    """The slice of the keyring backend API this store needs (the real keyring and test fakes).

    Parameter names mirror `keyring.backend.KeyringBackend` so the real backend and the fakes
    are structurally assignable.
    """

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class KeyringKeyStore:
    """A generation-addressed key ring stored in the platform keyring.

    Each generation lives under `<namespace> / gen-<key_id>` as a lowercase-hex string; the
    index entry `<namespace> / generations` is the JSON list the keyring cannot otherwise give
    back. **The index is the source of truth**, with one deliberate exception: **generation 1**,
    the master generation.

    For every other generation `put` writes the key before the index and `drop` removes the index
    entry before the key, so a crash at either point leaves an *orphan* key the index does not
    list — which `get` hides and a later `put` overwrites. That is what keeps an abandoned,
    distributed generation from answering `get(n)` for another machine's genuine generation `n`.

    Gen 1 is never wrapped, distributed, or dropped, so it can never be an abandoned rotation, and
    hiding it would let a lost index entry turn the live master key into an entry `init` then
    overwrites. For gen 1 the raw entry always counts, so `init` refuses and `put` cannot clobber
    it; the only way an index-less gen 1 arises is `init`'s own `put` crashing, and `init --import`
    repairs it.

    The index is read-modify-write, so `put` is only safe while the caller holds the repo lock
    (`docs/design/01-sync-model.md`).
    """

    def __init__(self, namespace: str, backend: _Backend | None = None) -> None:
        self._namespace = namespace
        self._backend = backend

    def _active_backend(self) -> _Backend:
        if self._backend is None:
            import keyring

            self._backend = keyring.get_keyring()
        return self._backend

    @staticmethod
    def _key_username(key_id: int) -> str:
        return f"gen-{key_id}"

    def _read_index(self) -> tuple[int, ...]:
        raw = self._active_backend().get_password(self._namespace, INDEX_USERNAME)
        if raw is None:
            return ()
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KeyStoreCorrupt(f"the keyring index for {self._namespace!r} is not valid JSON") from exc
        if (
            not isinstance(values, list)
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= UINT32_MAX for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise KeyStoreCorrupt(f"the keyring index for {self._namespace!r} is not a list of unique generation ids")
        return tuple(sorted(values))

    def generations(self) -> tuple[int, ...]:
        """Return the stored generation ids, ascending."""
        return self._read_index()

    def get(self, key_id: int) -> bytes | None:
        """Return generation `key_id`, or `None` when this machine does not hold it.

        The index decides **presence**, with one deliberate exception: **generation 1**, which is
        never wrapped, distributed, or dropped, so its raw entry always counts — that is what keeps
        a lost index entry from turning the live master key into an entry `init` overwrites. Every
        other generation whose key entry the index does not list is an orphan a crash left, and it
        is invisible here: that keeps an abandoned, distributed generation from answering `get(n)`
        for another machine's genuine generation `n` (``docs/design/05-key-generations.md``), and
        lets a later `put` repair the orphan instead of colliding with it. A present-but-unreadable
        entry raises `KeyStoreCorrupt`, not a bare `ValueError`, so a caller that branches on
        `CryptoError` sees "re-import it" rather than a crash.
        """
        if key_id != 1 and key_id not in self._read_index():
            return None
        raw = self._active_backend().get_password(self._namespace, self._key_username(key_id))
        if raw is None:
            return None
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise KeyStoreCorrupt(f"the stored generation {key_id} is not valid hex") from exc
        if len(key) != KEY_SIZE:
            raise KeyStoreCorrupt(f"the stored generation {key_id} is {len(key)} bytes, expected {KEY_SIZE}")
        return key

    def put(self, key_id: int, key: bytes) -> None:
        """Store generation `key_id`. Raises on failure."""
        if key_id < 1:
            raise ValueError(f"generation id must be >= 1, got {key_id}")
        if len(key) != KEY_SIZE:
            raise ValueError(f"a generation key must be {KEY_SIZE} bytes, got {len(key)}")
        existing = self.get(key_id)
        if existing is not None and existing != key:
            # Two keys for one generation id is a hard error, never something to reconcile:
            # published objects are already bound to one of them
            # (docs/design/05-key-generations.md). For generation 1 this is also what stops a fresh
            # `init` from clobbering a live master key whose index entry was lost; for any other
            # generation only an *indexed* id can conflict, since an unindexed orphan is invisible
            # to `get` and repaired by the write below.
            raise ValueError(f"generation {key_id} already holds a different key")
        # A corrupt entry that `get` can see (indexed, or generation 1) raises above; a corrupt
        # *orphan* is invisible and overwritten here, repairing it.
        if existing is None:
            # A fresh generation, or an orphan a crash left: write the key before the index, so a
            # crash keeps leaving an orphan the index hides.
            self._active_backend().set_password(self._namespace, self._key_username(key_id), key.hex())
        self._index(key_id)

    def _index(self, key_id: int) -> None:
        generations = self._read_index()
        if key_id in generations:
            return
        updated = sorted((*generations, key_id))
        self._active_backend().set_password(self._namespace, INDEX_USERNAME, json.dumps(updated))

    def drop(self, key_id: int) -> None:
        """Remove generation `key_id` from the ring, leaving the store unchanged when absent.

        Generation 1 is refused: it is the master generation, and a ring without it cannot be
        exported for recovery (``docs/design/05-key-generations.md``). The index is updated
        **before** the key entry is removed, so a crash between the two leaves at worst an
        orphaned key the ring does not list — never an index entry whose key is missing, which
        would make every later export or rotation fail as `KeyStoreCorrupt`.
        """
        if key_id == 1:
            raise ValueError("generation 1 is the master generation and cannot be dropped")
        generations = self._read_index()
        if key_id not in generations:
            return
        backend = self._active_backend()
        backend.set_password(
            self._namespace, INDEX_USERNAME, json.dumps([entry for entry in generations if entry != key_id])
        )
        if backend.get_password(self._namespace, self._key_username(key_id)) is not None:
            # A prior crash can leave the key without its index entry; deleting an absent entry
            # raises on macOS, so only delete one that is really there.
            backend.delete_password(self._namespace, self._key_username(key_id))
