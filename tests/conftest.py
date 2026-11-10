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
        # Mirror the real store: a second, different key for one generation id is a hard
        # error (docs/design/05-key-generations.md), so a test cannot rely on silent
        # overwrite and miss it.
        existing = self._keys.get(key_id)
        if existing is not None and existing != key:
            raise ValueError(f"generation {key_id} already holds a different key")
        self._keys[key_id] = key

    def drop(self, key_id: int) -> None:
        # Mirror the real store: generation 1 is the master generation and is never dropped.
        if key_id == 1:
            raise ValueError("generation 1 is the master generation and cannot be dropped")
        self._keys.pop(key_id, None)


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


@pytest.fixture(scope="session")
def profile_state_db():
    """A factory for fixture databases shaped like an app's `profile-state.db` (the layout Step 0 recorded on #55).

    Builds the schema in `tmp_path` only, never near a real user database. The keys in a payload are TEST-ONLY
    stand-ins: the real preset's fixtures take their key names from the real app. Every knob a boundary test needs
    (a wrong `user_version`, `profile_id`, `domain_version` or hash) is a parameter, and a second `ui` row with
    `legacy_json_acceptance` stands for the rest of the database a write must leave alone.
    """
    import hashlib
    import json
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    def _make(
        path: Path,
        *,
        settings: dict | str | None = None,
        profile_id: str | None = None,
        user_version: int = 3,
        domain_version: int = 1,
        revision: int = 7,
        row_revision: int | None = None,
        stored_hash: str | None = None,
        with_settings_row: bool = True,
    ) -> Path:
        payload = (
            settings
            if isinstance(settings, str)
            else json.dumps({"theme": "dark", "fontSize": 13} if settings is None else settings, separators=(",", ":"))
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("CREATE TABLE profile_state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE profile_state_documents (domain TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "domain_version INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                "content_hash TEXT NOT NULL)"
            )
            meta = {
                "profile_id": path.parent.name if profile_id is None else profile_id,
                "revision": str(revision),
                "legacy_json_acceptance": "unchanged-marker",
            }
            connection.executemany("INSERT INTO profile_state_meta VALUES (?, ?)", list(meta.items()))
            ui = json.dumps({"window": {"x": 1}}, separators=(",", ":"))
            connection.execute(
                "INSERT INTO profile_state_documents VALUES ('ui', ?, 1, 3, 1000, ?)",
                (ui, hashlib.sha256(ui.encode()).hexdigest()),
            )
            if with_settings_row:
                connection.execute(
                    "INSERT INTO profile_state_documents VALUES ('settings', ?, ?, ?, 1000, ?)",
                    (
                        payload,
                        domain_version,
                        revision if row_revision is None else row_revision,
                        hashlib.sha256(payload.encode()).hexdigest() if stored_hash is None else stored_hash,
                    ),
                )
            connection.execute(f"PRAGMA user_version = {int(user_version)}")
        return path

    return _make


_FAKE_DEFAULTS = '''#!{python}
"""A stand-in for /usr/bin/defaults that keeps each domain as an XML plist under $HOME/Library/Preferences.

It reproduces what the issue-58 Step 0 probe saw on a scratch domain: `export` of a missing domain prints an
empty `<dict/>` and exits 0, `import` merges per top-level key (a nested dict is replaced whole), `delete` removes
one key (rc 1 when there is none), and malformed input exits 1 and changes nothing. Every call is appended to
$HOME/.fake-defaults-calls.jsonl.
"""
import json, os, plistlib, sys
home = os.environ["HOME"]
argv = sys.argv[1:]
stdin = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
with open(os.path.join(home, ".fake-defaults-calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": argv, "stdin": stdin.decode("utf-8", "replace")}}) + "\\n")
command, domain = argv[0], argv[1]
path = os.path.join(home, "Library", "Preferences", domain + ".plist")
current = plistlib.load(open(path, "rb")) if os.path.exists(path) else {{}}
refuse = os.path.join(home, ".fake-defaults-refuse")
if os.path.exists(refuse) and command in open(refuse, encoding="utf-8").read().split():
    sys.exit(1)
if command == "export":
    sys.stdout.buffer.write(plistlib.dumps(current, fmt=plistlib.FMT_XML))
elif command == "import":
    try:
        incoming = plistlib.loads(stdin)
    except Exception:
        sys.exit(1)
    if not isinstance(incoming, dict):
        sys.exit(1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keep = os.path.join(home, ".fake-defaults-drop-import")
    if not os.path.exists(keep):
        plistlib.dump(current | incoming, open(path, "wb"), fmt=plistlib.FMT_XML)
elif command == "delete":
    if argv[2] not in current:
        sys.exit(1)
    current.pop(argv[2])
    plistlib.dump(current, open(path, "wb"), fmt=plistlib.FMT_XML)
else:
    sys.exit(2)
'''


@pytest.fixture
def fake_defaults(tmp_path):
    """A fake `defaults` executable and the helpers to seed a domain and read back what it was asked to do.

    The tests never reach the real `defaults` or `cfprefsd`: the store under test is handed this path. HOME is
    the temp home, so the domain's "plist" is a file under it (the real one is binary; the fake keeps XML).
    """
    import json
    import plistlib
    import stat
    import sys
    from pathlib import Path

    executable = tmp_path / "bin" / "defaults"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(_FAKE_DEFAULTS.format(python=sys.executable), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    class _Fake:
        path = str(executable)

        @staticmethod
        def seed(home: Path, domain: str, values: dict) -> Path:
            plist = home / "Library" / "Preferences" / f"{domain}.plist"
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_bytes(plistlib.dumps(values, fmt=plistlib.FMT_XML))
            return plist

        @staticmethod
        def values(home: Path, domain: str) -> dict:
            return plistlib.loads((home / "Library" / "Preferences" / f"{domain}.plist").read_bytes())

        @staticmethod
        def calls(home: Path) -> list[dict]:
            log = home / ".fake-defaults-calls.jsonl"
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

        @staticmethod
        def refuse(home: Path, *commands: str) -> None:
            (home / ".fake-defaults-refuse").write_text(" ".join(commands), encoding="utf-8")

        @staticmethod
        def drop_imports(home: Path) -> None:
            (home / ".fake-defaults-drop-import").write_text("", encoding="utf-8")

    return _Fake


@pytest.fixture(autouse=True)
def _no_real_defaults(monkeypatch):
    """No test can run the real `/usr/bin/defaults`: it ignores `HOME` and would read the developer's own domains.

    A test that needs a tool takes `fake_defaults` and hands its path to the store or patches this constant again.
    """
    monkeypatch.setattr(
        "syncintosh.system.defaults_domains.DEFAULTS_EXECUTABLE", "/nonexistent/never-the-real-defaults"
    )
