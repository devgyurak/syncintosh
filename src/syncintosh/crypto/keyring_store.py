# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `KeyStore` backed by the platform keyring.

The keyring has no enumeration API, so a single index entry lists the generation ids this
machine holds. No I/O happens in the constructor (``AGENTS.md`` §2.6): the backend is resolved
on the first call, so building the store never touches the keychain.
"""

from __future__ import annotations

import json
from typing import Protocol

KEY_SIZE = 32
INDEX_USERNAME = "generations"


class _Backend(Protocol):
    """The slice of the keyring backend API this store needs (the real keyring and test fakes).

    Parameter names mirror `keyring.backend.KeyringBackend` so the real backend and the fakes
    are structurally assignable.
    """

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


class KeyringKeyStore:
    """A generation-addressed key ring stored in the platform keyring.

    Each generation lives under `<namespace> / gen-<key_id>` as a lowercase-hex string; the
    index entry `<namespace> / generations` is the JSON list the keyring cannot otherwise give
    back. `put` writes the key before the index, so a crash loses an index entry at worst, not
    a key.
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
            raise ValueError(f"the keyring index for {self._namespace!r} is not valid JSON") from exc
        return tuple(sorted(int(value) for value in values))

    def generations(self) -> tuple[int, ...]:
        """Return the stored generation ids, ascending."""
        return self._read_index()

    def get(self, key_id: int) -> bytes | None:
        """Return generation `key_id`, or `None` when this machine does not hold it."""
        raw = self._active_backend().get_password(self._namespace, self._key_username(key_id))
        return None if raw is None else bytes.fromhex(raw)

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
            # (docs/design/05-key-generations.md). The dedicated error lands with #8.
            raise ValueError(f"generation {key_id} already holds a different key")
        if existing is None:
            self._active_backend().set_password(self._namespace, self._key_username(key_id), key.hex())
        self._index(key_id)

    def _index(self, key_id: int) -> None:
        generations = self._read_index()
        if key_id in generations:
            return
        updated = sorted((*generations, key_id))
        self._active_backend().set_password(self._namespace, INDEX_USERNAME, json.dumps(updated))
