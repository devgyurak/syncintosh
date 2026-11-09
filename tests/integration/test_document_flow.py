# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a settings document kept in a database row, synced between two Macs as a virtual file (issue #55).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The preset is TEST-ONLY
(`acme`, a made-up app); the real Orca preset is a later change. Each Mac's database is a fixture built in `tmp_path`
(`profile_state_db`), so no real user database is read or written. The flow is the real one: collect reads the row
through the router, the object is sealed and committed, and apply on the other Mac writes the row under the app's
own protocol.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import SyncConfig
from syncintosh.core.documents import ORCA_PROFILE_STATE, DocumentSource
from syncintosh.core.errors import DocumentStale
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.policy import policy_digest
from syncintosh.core.ports import LiveFiles
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.diff import diff_repo
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.document_files import open_live_files
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.lock import FileLock
from syncintosh.system.pending import DirectoryPendingStore
from syncintosh.system.repo_files import FileRepo
from syncintosh.system.secret_scan import PatternSecretScanner
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
DATABASE = "Library/Application Support/acme/profiles/main/state.db"
VIEW = "Library/Application Support/acme/profiles/main/@state/settings.json"
PLAIN = "Library/Application Support/acme/notes.txt"
SOURCE = DocumentSource.create(kind=ORCA_PROFILE_STATE, database=DATABASE, domain="settings", view=VIEW)
ACME = Preset.create(
    name="acme",
    files=(PresetFile.create(path=VIEW, format="text"), PresetFile.create(path=PLAIN, format="text")),
    bundle_id="com.example.acme",
    documents=(SOURCE,),
)


@dataclass(frozen=True)
class _TestCatalog(BuiltinCatalog):
    """The shipped catalog's lookup over the test preset, with the one-entry policy history a shipped preset has."""

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        preset = self.get(name)
        return None if preset is None else (policy_digest(preset),)


CATALOG = _TestCatalog.create((ACME,))


@dataclass
class Machine:
    home: Path
    repo: Path
    ledger: Path
    pending: Path
    lock: Path
    config_file: Path
    store: Any
    remote: str
    machine_id: str
    recovery: Path | None = None
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))

    @property
    def database(self) -> Path:
        return self.home / DATABASE


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _machine(tmp_path: Path, make_store, name: str, remote: str) -> Machine:
    home = tmp_path / name / "home"
    state = home / ".local" / "share" / "syncintosh"
    return Machine(
        home=home,
        repo=state / "repo",
        ledger=state / "applied.json",
        pending=state / "pending",
        lock=state / "sync.lock",
        config_file=home / ".config" / "syncintosh" / "config.json",
        store=make_store(),
        remote=remote,
        machine_id=name,
    )


def _initialize(machine: Machine, *, import_from: Path | None = None) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    save_config(
        new_machine_config(
            remote=machine.remote, machine_id=machine.machine_id, preset_names=("acme",), catalog=CATALOG
        ),
        machine.config_file,
    )
    transport = GitCLITransport(machine.repo)
    if import_from is None:
        transport.init()
        machine.recovery = machine.home / "recovery.sntr"
        init_new_repo(
            store=machine.store,
            codec=machine.codec,
            files=FileRepo(machine.repo),
            live=SystemLiveFiles(),
            recovery_path=str(machine.recovery),
            passphrase=PASSPHRASE,
        )
        return
    transport.clone(machine.remote)
    blob = transport.read_blob(transport.head() or "", COMMITMENTS_PATH)
    import_ring(
        store=machine.store,
        codec=machine.codec,
        blob=import_from.read_bytes(),
        passphrase=PASSPHRASE,
        published={} if blob is None else loads_commitments(blob),
    )


def _run(machine: Machine, *, wrap: Callable[[LiveFiles], LiveFiles] = lambda live: live) -> SyncReport:
    active = GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    config: SyncConfig = load_config(machine.config_file)
    return sync(
        transport=active,
        files=FileRepo(machine.repo),
        cipher=cipher,
        scanner=PatternSecretScanner(),
        store=machine.store,
        codec=machine.codec,
        wrapper=cipher,
        driver=TextMergeDriver(),
        adapters={},
        catalog=CATALOG,
        live=wrap(open_live_files(SystemLiveFiles(), str(machine.home), CATALOG.documents())),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        source=SecureFileIdSource(),
        config=config,
        home=str(machine.home),
        branch=active.branch() or BRANCH,
    )


def _query(machine: Machine, sql: str, *args: object) -> list[tuple]:
    with closing(sqlite3.connect(machine.database)) as connection:
        return connection.execute(sql, args).fetchall()


def _edit_like_the_app(machine: Machine, payload: str) -> None:
    """What the app itself does when it saves: the row, its hash, and both revisions, in one transaction."""
    revision = int(_query(machine, "SELECT value FROM profile_state_meta WHERE key = 'revision'")[0][0]) + 1
    with closing(sqlite3.connect(machine.database)) as connection:
        connection.execute(
            "UPDATE profile_state_documents SET payload=?, revision=?, content_hash=? WHERE domain='settings'",
            (payload, revision, hashlib.sha256(payload.encode()).hexdigest()),
        )
        connection.execute("UPDATE profile_state_meta SET value=? WHERE key='revision'", (str(revision),))
        connection.commit()


def _write_plain(machine: Machine, text: str) -> None:
    (machine.home / PLAIN).parent.mkdir(parents=True, exist_ok=True)
    (machine.home / PLAIN).write_text(text, encoding="utf-8")


def _manifest_paths(machine: Machine) -> list[str]:
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return sorted(record.path for record in manifest.records.values())


def _pair(tmp_path: Path, make_store, profile_state_db, *, b_database: bool = True, **b_options: Any):
    """A has synced a row; B is initialised with its own database (or none)."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    profile_state_db(a.database, settings={"theme": "dark", "fontSize": 13})
    _write_plain(a, "hello\n")
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    if b_database:
        profile_state_db(b.database, settings={"theme": "dark", "fontSize": 13}, **b_options)
        _write_plain(b, "hello\n")
    return a, b


def test_a_row_edited_on_one_mac_reaches_the_others_database_under_the_apps_protocol(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given — both in sync; A's app saves a new value
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    other_rows = _query(b, "SELECT * FROM profile_state_documents WHERE domain = 'ui'")
    marker = _query(b, "SELECT value FROM profile_state_meta WHERE key = 'legacy_json_acceptance'")
    before = int(_query(b, "SELECT value FROM profile_state_meta WHERE key = 'revision'")[0][0])
    _edit_like_the_app(a, '{"theme":"light","fontSize":14,"zoom":1.5}')

    # when
    _run(a)
    report = _run(b)

    # then — B's row carries A's value, its hash and both revisions are the app's own, and nothing else moved
    ((payload, domain_version, row_revision, stored_hash),) = _query(
        b, "SELECT payload, domain_version, revision, content_hash FROM profile_state_documents WHERE domain='settings'"
    )
    assert report.has_conflicts is False
    assert payload == '{"theme":"light","fontSize":14,"zoom":1.5}'
    assert (domain_version, row_revision) == (1, before + 1)
    assert stored_hash == hashlib.sha256(payload.encode()).hexdigest()
    assert _query(b, "SELECT value FROM profile_state_meta WHERE key = 'revision'") == [(str(before + 1),)]
    assert _query(b, "SELECT * FROM profile_state_documents WHERE domain = 'ui'") == other_rows
    assert _query(b, "SELECT value FROM profile_state_meta WHERE key = 'legacy_json_acceptance'") == marker


def test_the_record_carries_the_virtual_path_and_the_repo_names_no_database(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store, profile_state_db)

    # when
    names = sorted(p.name for p in (a.repo / "objects").iterdir())

    # then — the manifest holds the view path; no filename or tracked path names the database or the app
    assert _manifest_paths(a) == [PLAIN, VIEW]
    assert [n for n in names if "state" in n or "acme" in n] == []
    assert (a.home / VIEW).exists() is False


def test_a_sync_that_changes_nothing_does_not_bump_the_apps_revision(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    before = _query(b, "SELECT * FROM profile_state_meta ORDER BY key")

    # when
    _run(b)
    _run(a)
    _run(b)

    # then
    assert _query(b, "SELECT * FROM profile_state_meta ORDER BY key") == before


def test_a_mac_whose_app_has_not_made_its_database_defers_and_creates_nothing(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given — the app is not installed here
    _, b = _pair(tmp_path, memory_key_store, profile_state_db, b_database=False)

    # when
    report = _run(b)

    # then
    assert [d.path for d in report.deferred if d.path == VIEW] == [VIEW]
    assert (b.home / "Library").exists() is False


class _StaleOnWrite:
    """A `LiveFiles` over another whose document write finds the row moved, as when the app saves mid-cycle."""

    def __init__(self, inner: LiveFiles) -> None:
        self._inner = inner

    def read(self, path: str) -> bytes | None:
        return self._inner.read(path)

    def write(self, path: str, data: bytes) -> None:
        if path.endswith("@state/settings.json"):
            raise DocumentStale("the document changed since it was read, so it was not written")
        self._inner.write(path, data)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def is_symlink(self, path: str) -> bool:
        return self._inner.is_symlink(path)

    def list_dir(self, path: str) -> tuple[str, ...]:
        return self._inner.list_dir(path)

    def remove(self, path: str) -> None:
        self._inner.remove(path)


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version = 4",
        "UPDATE profile_state_meta SET value = 'someone-else' WHERE key = 'profile_id'",
        "UPDATE profile_state_documents SET domain_version = 2 WHERE domain = 'settings'",
        "UPDATE profile_state_documents SET content_hash = '0' WHERE domain = 'settings'",
    ],
)
def test_an_unsupported_database_is_one_refused_file_and_deletes_nothing(
    tmp_path, memory_key_store, profile_state_db, statement: str
) -> None:
    # given — in sync; then B's database becomes unsupported (an app update) while A changes both files
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    published = _manifest_paths(b)
    _edit_like_the_app(a, '{"theme":"light"}')
    _write_plain(a, "from a\n")
    _run(a)
    with closing(sqlite3.connect(b.database)) as connection:
        connection.execute(statement)
        connection.commit()
    unchanged = _query(b, "SELECT * FROM profile_state_documents ORDER BY domain")
    unchanged_meta = _query(b, "SELECT * FROM profile_state_meta ORDER BY key")

    # when — the sync does not stop: the plain file still moves, and the document is reported by name
    report = _run(b)

    # then
    entries = JsonLedgerStore(b.ledger).load()
    assert [r.path for r in report.refused if r.blocking] == [VIEW]
    assert tuple(report.applied) == (PLAIN,)
    assert (b.home / PLAIN).read_text(encoding="utf-8") == "from a\n"
    # the database was left alone, the record is still in the repo, and nothing was read as a deletion
    assert _query(b, "SELECT * FROM profile_state_documents ORDER BY domain") == unchanged
    assert _query(b, "SELECT * FROM profile_state_meta ORDER BY key") == unchanged_meta
    assert _manifest_paths(b) == published == _manifest_paths(a)
    assert [entry for entry in entries.values() if entry.suppressed] == []


def test_the_refused_document_arrives_once_the_database_is_supported_again(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given — B was refused for a cycle while A moved on
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    with closing(sqlite3.connect(b.database)) as connection:
        connection.execute("PRAGMA user_version = 4")
    _edit_like_the_app(a, '{"theme":"light"}')
    _run(a)
    _run(b)

    # when — the version is back
    with closing(sqlite3.connect(b.database)) as connection:
        connection.execute("PRAGMA user_version = 3")
    report = _run(b)

    # then — no conflict, no re-created record: the document just applies
    assert report.has_conflicts is False
    assert _query(b, "SELECT payload FROM profile_state_documents WHERE domain = 'settings'") == [
        ('{"theme":"light"}',)
    ]


def test_a_document_that_moved_between_the_read_and_the_write_is_a_non_blocking_per_file_report(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    before = _query(b, "SELECT * FROM profile_state_documents WHERE domain = 'settings'")
    _edit_like_the_app(a, '{"theme":"light"}')
    _write_plain(a, "from a\n")
    _run(a)

    # when
    report = _run(b, wrap=_StaleOnWrite)

    # then — the plain file applied, the document was not written, and the next plain cycle retries it
    assert [(r.path, r.blocking) for r in report.refused] == [(VIEW, False)]
    assert tuple(report.applied) == (PLAIN,)
    assert _query(b, "SELECT * FROM profile_state_documents WHERE domain = 'settings'") == before
    _run(b)
    assert _query(b, "SELECT payload FROM profile_state_documents WHERE domain = 'settings'") == [
        ('{"theme":"light"}',)
    ]


def test_diff_names_a_refused_document_as_deferred_and_still_compares_the_others(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given — B's database is unsupported
    _, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    with closing(sqlite3.connect(b.database)) as connection:
        connection.execute("PRAGMA user_version = 4")
    transport = GitCLITransport(b.repo)

    # when
    report = diff_repo(
        transport=transport,
        cipher=EnvelopeCipher(b.store),
        live=open_live_files(SystemLiveFiles(), str(b.home), CATALOG.documents()),
        config=load_config(b.config_file),
        home=str(b.home),
        deny=SystemDenyList(),
        branch=transport.branch() or BRANCH,
        catalog=CATALOG,
        adapters={},
        validators={},
    )

    # then
    assert sorted((entry.path, entry.status) for entry in report.entries) == [(PLAIN, "in-sync"), (VIEW, "deferred")]
