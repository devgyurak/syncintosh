# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the Orca preset, syncing the `settings` row of a profile database between two Macs (issue #55).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The catalog is the one the
CLI wires. Each Mac's `profile-state.db` is a fixture built in `tmp_path` (`profile_state_db`) with the key names a real
Orca 1.4.219 has (`tests/fixtures/orca/settings.json`; the values are invented), so no real database is read or written.
The running-app and installed-app ports are fakes, and `orcad` is a pid file next to a fake process table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import SyncConfig
from syncintosh.core.errors import DocumentRefused
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, object_path
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.orca import BUNDLE_ID, DATABASE, EXCLUDED, SYNCED, VIEW
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.document_files import open_live_files
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.lock import FileLock
from syncintosh.system.orca_daemon import DAEMON_DIRECTORY, OrcaDaemonRunningApps
from syncintosh.system.pending import DirectoryPendingStore
from syncintosh.system.repo_files import FileRepo
from syncintosh.system.secret_scan import PatternSecretScanner
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
CATALOG = BuiltinCatalog.default()
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "orca" / "settings.json"
SYNCED_NAMES = {key for (key,) in SYNCED}
EXCLUDED_NAMES = sorted(key for (key,) in EXCLUDED if isinstance(key, str))
A_COOKIE = "invented-cookie-of-machine-a"
B_COOKIE = "invented-cookie-of-machine-b"
INSIDE = "/Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper"


def _settings(**changes: Any) -> dict:
    return {**json.loads(FIXTURE.read_text(encoding="utf-8")), **changes}


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


class FakeRunning:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer

    def running(self, bundle_id: str) -> bool | None:
        return self.answer


class FakeApps:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer

    def installed(self, bundle_id: str) -> bool | None:
        return self.answer


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
            remote=machine.remote, machine_id=machine.machine_id, preset_names=("orca",), catalog=CATALOG
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


def _run(machine: Machine, *, running: Any = None, apps: Any = NO_APP_INFO) -> SyncReport:
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
        adapters={"jsonc": JsoncAdapter()},
        catalog=CATALOG,
        live=open_live_files(SystemLiveFiles(), str(machine.home), CATALOG.documents()),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        source=SecureFileIdSource(),
        config=config,
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        apps=apps,
        running=FakeRunning(False) if running is None else running,
    )


def _query(machine: Machine, sql: str, *args: object) -> list[tuple]:
    with closing(sqlite3.connect(machine.database)) as connection:
        return connection.execute(sql, args).fetchall()


def _row(machine: Machine) -> dict:
    return json.loads(_query(machine, "SELECT payload FROM profile_state_documents WHERE domain='settings'")[0][0])


def _edit_like_the_app(machine: Machine, value: dict) -> None:
    """What the app does when it saves: the row, its hash and both revisions, in one transaction."""
    payload = json.dumps(value, separators=(",", ":"))
    revision = int(_query(machine, "SELECT value FROM profile_state_meta WHERE key = 'revision'")[0][0]) + 1
    with closing(sqlite3.connect(machine.database)) as connection:
        connection.execute(
            "UPDATE profile_state_documents SET payload=?, revision=?, content_hash=? WHERE domain='settings'",
            (payload, revision, hashlib.sha256(payload.encode()).hexdigest()),
        )
        connection.execute("UPDATE profile_state_meta SET value=? WHERE key='revision'", (str(revision),))
        connection.commit()


def _sealed_object(machine: Machine) -> str:
    """The one object a machine published, opened: what an attacker with the key would read."""
    transport = GitCLITransport(machine.repo)
    head = transport.head() or ""
    cipher = EnvelopeCipher(machine.store)
    manifest = loads_manifest(
        cipher.open(transport.read_blob(head, MANIFEST_PATH) or b"", domain="manifest", file_id="")
    )
    (file_id,) = manifest.records
    sealed = transport.read_blob(head, object_path(file_id)) or b""
    return cipher.open(sealed, domain="object", file_id=file_id).decode("utf-8")


def _pair(tmp_path: Path, make_store, profile_state_db, *, b_database: bool = True, **b_options: Any):
    """A has synced a row with a cookie of its own; B has the same preferences and its own machine values."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    profile_state_db(a.database, settings=_settings(opencodeSessionCookie=A_COOKIE, httpProxyUrl="http://a:secret@a"))
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    if b_database:
        profile_state_db(
            b.database,
            settings=_settings(
                opencodeSessionCookie=B_COOKIE,
                httpProxyUrl="http://b:secret@b",
                workspaceDir="/Users/machine-b/work",
                machineName="machine-b",
            ),
            **b_options,
        )
    return a, b


def test_a_preference_edited_on_one_mac_reaches_the_others_row_and_its_local_values_stay(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given — both in sync; A's app saves a new theme
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    before = int(_query(b, "SELECT value FROM profile_state_meta WHERE key = 'revision'")[0][0])
    marker = _query(b, "SELECT value FROM profile_state_meta WHERE key = 'legacy_json_acceptance'")
    other_rows = _query(b, "SELECT * FROM profile_state_documents WHERE domain = 'ui'")
    _edit_like_the_app(a, {**_row(a), "theme": "light", "terminalFontSize": 17})

    # when
    _run(a)
    report = _run(b)

    # then — B has A's preferences and the values that are B's own
    row = _row(b)
    assert report.has_conflicts is False
    assert (row["theme"], row["terminalFontSize"]) == ("light", 17)
    assert (row["workspaceDir"], row["machineName"]) == ("/Users/machine-b/work", "machine-b")
    assert (row["opencodeSessionCookie"], row["httpProxyUrl"]) == (B_COOKIE, "http://b:secret@b")
    # and the app's own protocol held: hash, both revisions, and nothing else moved
    ((payload, row_revision, stored_hash),) = _query(
        b, "SELECT payload, revision, content_hash FROM profile_state_documents WHERE domain='settings'"
    )
    assert row_revision == before + 1
    assert stored_hash == hashlib.sha256(payload.encode()).hexdigest()
    assert _query(b, "SELECT value FROM profile_state_meta WHERE key = 'revision'") == [(str(before + 1),)]
    assert _query(b, "SELECT value FROM profile_state_meta WHERE key = 'legacy_json_acceptance'") == marker
    assert _query(b, "SELECT * FROM profile_state_documents WHERE domain = 'ui'") == other_rows


def test_the_published_object_holds_no_credential_and_no_key_outside_the_allowlist(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store, profile_state_db)

    # when — the object is opened with the key, which is the worst case
    text = _sealed_object(a)
    keys = set(json.loads(text))

    # then
    assert sorted(keys - SYNCED_NAMES) == []
    assert sorted(set(EXCLUDED_NAMES) & keys) == []
    assert [secret for secret in (A_COOKIE, "a:secret", "/invented/path") if secret in text] == []


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


def test_a_local_only_change_is_never_published(tmp_path, memory_key_store, profile_state_db) -> None:
    # given — B's app saves only values this Mac keeps to itself
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    _edit_like_the_app(b, {**_row(b), "workspaceDir": "/Users/machine-b/elsewhere", "telemetry": {"installId": "x"}})
    head = GitCLITransport(b.repo).head()

    # when
    _run(b)

    # then — no commit, and A's row is untouched
    assert GitCLITransport(b.repo).head() == head
    assert _row(a)["workspaceDir"] != "/Users/machine-b/elsewhere"


@pytest.mark.parametrize("answer", [True, None])
def test_an_open_or_unknowable_orca_defers_the_apply_and_collect_proceeds(
    tmp_path, memory_key_store, profile_state_db, answer: bool | None
) -> None:
    # given — A saves a new theme; B's Orca is open and B saves a preference of its own
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    _edit_like_the_app(a, {**_row(a), "theme": "light"})
    _run(a)
    _edit_like_the_app(b, {**_row(b), "terminalFontSize": 21})
    before = int(_query(b, "SELECT value FROM profile_state_meta WHERE key = 'revision'")[0][0])

    # when
    report = _run(b, running=FakeRunning(answer))

    # then — the apply waits (the app's revision is not touched), and B's edit was still published
    assert [d for d in report.deferred if d.path == VIEW and "quit it" in d.reason] != []
    assert _row(b)["theme"] != "light"
    assert _query(b, "SELECT value FROM profile_state_meta WHERE key = 'revision'") == [(str(before),)]
    _run(a)
    assert _row(a)["terminalFontSize"] == 21


def test_a_deferred_apply_lands_once_orca_has_quit(tmp_path, memory_key_store, profile_state_db) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    _edit_like_the_app(a, {**_row(a), "theme": "light"})
    _run(a)
    _run(b, running=FakeRunning(True))

    # when
    report = _run(b, running=FakeRunning(False))

    # then
    assert VIEW in report.applied
    assert _row(b)["theme"] == "light"


def _daemon(machine: Machine, table: dict[int, str]) -> OrcaDaemonRunningApps:
    directory = machine.home / DAEMON_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "daemon-v38.pid").write_text(json.dumps({"pid": 4242, "launchNonce": "n"}), encoding="utf-8")

    def _path(pid: int) -> str:
        if pid not in table:
            raise ProcessLookupError(pid)
        return table[pid]

    return OrcaDaemonRunningApps(FakeRunning(False), home=str(machine.home), bundle_id=BUNDLE_ID, process_path=_path)


def test_a_live_orcad_defers_the_apply_after_the_app_has_quit(tmp_path, memory_key_store, profile_state_db) -> None:
    # given — the window is closed (`lsappinfo` says no) but the terminal host still runs from inside Orca.app
    a, b = _pair(tmp_path, memory_key_store, profile_state_db)
    _run(b)
    _edit_like_the_app(a, {**_row(a), "theme": "light"})
    _run(a)

    # when
    held = _run(b, running=_daemon(b, {4242: INSIDE}))
    released = _run(b, running=_daemon(b, {}))

    # then — held while the daemon lives, applied once the pid is gone
    assert [d for d in held.deferred if d.path == VIEW and "quit it" in d.reason] != []
    assert VIEW in released.applied
    assert _row(b)["theme"] == "light"


def test_a_mac_without_orca_defers_with_the_download_page_and_creates_nothing(
    tmp_path, memory_key_store, profile_state_db
) -> None:
    # given — Orca is not installed on B, so there is no database
    _, b = _pair(tmp_path, memory_key_store, profile_state_db, b_database=False)

    # when
    report = _run(b, apps=FakeApps(False))

    # then — the shipped catalog's own metadata: a release page, no cask
    (deferral,) = [d for d in report.deferred if d.path == VIEW]
    assert deferral.reason == "the app is not installed on this machine"
    assert deferral.download_url == "https://github.com/stablyai/orca/releases"
    assert deferral.cask is None
    assert (b.home / "Library").exists() is False


@pytest.mark.parametrize(
    "options", [{"user_version": 2}, {"user_version": 4}, {"profile_id": "someone-else"}, {"domain_version": 2}]
)
def test_an_unsupported_orca_database_is_refused_and_left_alone(
    tmp_path, memory_key_store, profile_state_db, options: dict
) -> None:
    # given
    _, b = _pair(tmp_path, memory_key_store, profile_state_db, **options)
    before = _query(b, "SELECT * FROM profile_state_documents ORDER BY domain")

    # when & then
    with pytest.raises(DocumentRefused):
        _run(b)
    assert _query(b, "SELECT * FROM profile_state_documents ORDER BY domain") == before
