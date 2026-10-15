# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared test fixtures.

Every test runs against a throwaway HOME, a failing keychain, and a neutral git config so no
test can touch the developer's real settings, keychain, or commit-signing configuration
(``AGENTS.md`` §5.6). Network access is disabled globally via ``--disable-socket``.
"""

from __future__ import annotations

import os

import pytest


class MemoryKeyStore:
    """An in-memory KeyStore for tests; never touches the real keychain (``AGENTS.md`` §5.6)."""

    def __init__(self, keys: dict[int, bytes] | None = None) -> None:
        self._keys: dict[int, bytes] = dict(keys or {})

    def generations(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def get(self, key_id: int) -> bytes | None:
        return self._keys.get(key_id)

    def put(self, key_id: int, key: bytes) -> None:
        self._keys[key_id] = key


@pytest.fixture(scope="session")
def memory_key_store():
    """A factory for in-memory key rings, seeded with the given `{key_id: key}` map.

    Session-scoped (a stateless factory) so a `@given` test can call it: hypothesis refuses
    function-scoped fixtures in a property test, and each call still returns a fresh store.
    """

    def _make(keys: dict[int, bytes] | None = None) -> MemoryKeyStore:
        return MemoryKeyStore(keys)

    return _make


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Point HOME/XDG at a temp dir and neutralize the keychain and git config."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    # Never read or write the real login keychain during a test.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    # Ignore the user's global/system git config (signing keys, hooks) in a temp repo.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (home / ".local" / "share").mkdir(parents=True, exist_ok=True)
