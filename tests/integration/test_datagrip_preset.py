# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the DataGrip preset across two Macs on two releases (issue #54).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The installed app is a temp
`.app` bundle whose `Contents/Resources/product-info.json` has the shape a real DataGrip's has, found through a fake
locator; the running-app and installed-app ports are fakes. The catalog is the one the CLI wires. The settings files
are synthetic, shaped from the evidence in `presets/datagrip.py` and the intellij-community source.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _harness import run_sync

from syncintosh.adapters.xml import XmlAdapter
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.jetbrains import SYNC_CONFLICT
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import SyncReport
from syncintosh.system.app_config_dir import ProductInfoConfigDir
from syncintosh.system.config import save_config
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.repo_files import FileRepo
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
HOME = "Library/Application Support/JetBrains"
ROOT = f"{HOME}/@DataGrip"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jetbrains"
CATALOG = BuiltinCatalog.default()

EDITOR_FONT = f"{ROOT}/options/editor-font.xml"
GENERAL = f"{ROOT}/options/ide.general.xml"
KEYMAP = f"{ROOT}/options/mac/keymap.xml"
SCHEME = f"{ROOT}/colors/sample.icls"
DATABASE = f"{ROOT}/options/databaseSettings.xml"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


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
    release: str
    app: Path
    recovery: Path | None = None
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))


class FakeRunning:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer

    def running(self, bundle_id: str) -> bool | None:
        return self.answer


class FakeApps:
    """An `AppDetector` that says the app is installed (the config directory is read from the app's own metadata)."""

    def installed(self, bundle_id: str) -> bool | None:
        return True


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _install_app(root: Path, directory: str) -> Path:
    app = root / "DataGrip.app"
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    document = {"name": "DataGrip", "dataDirectoryName": directory, "productVendor": "JetBrains"}
    (app / "Contents" / "Resources" / "product-info.json").write_text(json.dumps(document), encoding="utf-8")
    return app


def _machine(tmp_path: Path, make_store, name: str, remote: str, release: str) -> Machine:
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
        release=release,
        app=_install_app(tmp_path / name / "apps", release),
    )


def _initialize(machine: Machine, *, import_from: Path | None = None, presets: tuple[str, ...] = ("datagrip",)) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    save_config(
        new_machine_config(remote=machine.remote, machine_id=machine.machine_id, preset_names=presets, catalog=CATALOG),
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


def _run(machine: Machine, *, running: FakeRunning | None = None, locate: tuple[str, ...] | None = None) -> SyncReport:
    found = (str(machine.app),) if locate is None else locate
    return run_sync(
        machine.home,
        machine.store,
        codec=machine.codec,
        adapters={"xml": XmlAdapter()},
        catalog=CATALOG,
        apps=FakeApps(),
        running=FakeRunning(False) if running is None else running,
        config_dirs=ProductInfoConfigDir(lambda _bundle: found, home=machine.home),
    )


def _folder(machine: Machine) -> Path:
    return machine.home / HOME / machine.release


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _records(machine: Machine) -> dict[str, str]:
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return {record.path: record.file_id for record in manifest.records.values()}


def _objects(machine: Machine) -> list[bytes]:
    cipher = EnvelopeCipher(machine.store)
    files = FileRepo(machine.repo)
    return [
        cipher.open(files.read(rel) or b"", domain="object", file_id=Path(rel).stem)
        for rel in files.list_paths("objects")
    ]


def _seed(machine: Machine) -> None:
    _write(_folder(machine) / "options" / "editor-font.xml", _fixture("editor-font.xml"))
    _write(_folder(machine) / "options" / "ide.general.xml", _fixture("ide.general.xml"))
    _write(_folder(machine) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml"))
    _write(_folder(machine) / "options" / "databaseSettings.xml", _fixture("databaseSettings.xml"))
    _write(_folder(machine) / "colors" / "sample.icls", _fixture("sample.icls"))


def _pair(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """A on 2026.2 has synced its settings; B on 2026.3 is initialised with its own, empty folder."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote, "DataGrip2026.2")
    _initialize(a)
    _seed(a)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote, "DataGrip2026.3")
    _initialize(b, import_from=a.recovery)
    _folder(b).mkdir(parents=True)
    return a, b


def test_two_macs_on_two_releases_converge_through_one_set_of_records(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)

    # when
    report = _run(b)

    # then — every file lands in B's own release folder and the records are A's, with no release in any name
    assert report.has_conflicts is False
    assert sorted(_records(b)) == sorted([EDITOR_FONT, GENERAL, KEYMAP, SCHEME, DATABASE])
    assert _records(b) == _records(a)
    assert (_folder(b) / "options" / "editor-font.xml").read_bytes() == _fixture("editor-font.xml")
    assert (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes() == _fixture("keymap.xml")
    assert (_folder(b) / "options" / "databaseSettings.xml").read_bytes() == _fixture("databaseSettings.xml")
    assert (b.home / HOME / "DataGrip2026.2").exists() is False
    assert [p.name for p in (a.repo / "objects").iterdir() if "2026" in p.name or "DataGrip" in p.name] == []


def test_the_machines_own_noise_is_never_sealed_and_survives_an_apply(tmp_path, memory_key_store) -> None:
    # given — B has its own scheme timestamps and its own Registry entry for the IDE-set key
    a, b = _pair(tmp_path, memory_key_store)
    mine = _fixture("ide.general.xml").replace(b"https://example.invalid/a", b"https://example.invalid/b")
    _write(_folder(b) / "options" / "ide.general.xml", mine)
    _write(_folder(a) / "options" / "editor-font.xml", _fixture("editor-font.xml").replace(b'"13"', b'"15"'))
    _run(a)

    # when
    _run(b)

    # then — the font reached B, B's own noise value is still its own, and no object holds a noise value
    assert b'value="15"' in (_folder(b) / "options" / "editor-font.xml").read_bytes()
    assert b"https://example.invalid/b" in (_folder(b) / "options" / "ide.general.xml").read_bytes()
    sealed = _objects(a)
    assert [o for o in sealed if b"trace.state.event" in o or b"2026-01-01T10:00:00" in o or b"2026.2.6.0" in o] == []


def test_a_change_on_either_release_reaches_the_other(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)

    # when — B (2026.3) edits a scheme, A (2026.2) syncs; A edits the keymap, B syncs
    _write(_folder(b) / "colors" / "sample.icls", _fixture("sample.icls").replace(b"ABCDEF", b"123456"))
    _run(b)
    _run(a)
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Other"))
    _run(a)
    _run(b)

    # then
    assert b"123456" in (_folder(a) / "colors" / "sample.icls").read_bytes()
    assert b"Other" in (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes()


def test_credential_shaped_files_beside_the_settings_collect_nothing(tmp_path, memory_key_store) -> None:
    # given — a folder that also holds the files that must never travel (synthetic stand-ins)
    a, _ = _pair(tmp_path, memory_key_store)
    folder = _folder(a)
    _write(folder / "DataGrip.key", b"do-not-sync-key")
    _write(folder / "c.kdbx", b"do-not-sync-kdbx")
    _write(folder / "c.pwd", b"do-not-sync-pwd")
    _write(folder / "options" / "dataSources.xml", b'<application><data-source url="jdbc:do-not-sync"/></application>')
    _write(
        folder / "options" / "dataSources.local.xml",
        b'<application><option name="user" value="do-not-sync"/></application>',
    )
    _write(folder / "options" / "security.xml", b'<application><option name="do-not-sync"/></application>')
    _write(folder / "options" / "recentProjects.xml", b'<application><option name="do-not-sync"/></application>')
    _write(folder / "ssl" / "ca.pem", b"do-not-sync-pem")
    _write(folder / "consoles" / "db" / "c.sql", b"select 'do-not-sync'")

    # when
    _run(a)

    # then — no record and no decrypted object holds any of it
    assert [path for path in _records(a) if "do-not-sync" in path or "kdbx" in path or "dataSources" in path] == []
    assert sorted(_records(a)) == sorted([EDITOR_FONT, GENERAL, KEYMAP, SCHEME, DATABASE])
    assert [o for o in _objects(a) if b"do-not-sync" in o] == []


# --------------------------------------------------------------------------- the app's own Settings Sync


def test_with_the_apps_own_sync_on_neither_collect_nor_apply_touches_that_machines_files(
    tmp_path, memory_key_store
) -> None:
    # given — B turns Settings Sync on and edits a file; A publishes a different edit
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    _write(_folder(b) / "options" / "settingsSync.xml", _fixture("settingsSync-on.xml"))
    local = _fixture("editor-font.xml").replace(b'"13"', b'"30"')
    _write(_folder(b) / "options" / "editor-font.xml", local)
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Remote"))
    _run(a)
    before = _records(a)

    # when
    report = _run(b)

    # then — B's edit was not collected, the remote edit was not applied, and the report says why
    assert _records(b) == before
    assert b"Remote" not in (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes()
    assert (_folder(b) / "options" / "editor-font.xml").read_bytes() == local
    assert {d.reason for d in report.deferred if d.path in (EDITOR_FONT, KEYMAP)} == {SYNC_CONFLICT}
    assert (_folder(b) / "options" / "settingsSync.xml").read_bytes() == _fixture("settingsSync-on.xml")
    assert [o for o in _objects(a) if b"syncEnabled" in o] == []


def test_with_the_apps_own_sync_off_the_files_flow(tmp_path, memory_key_store) -> None:
    # given
    _, b = _pair(tmp_path, memory_key_store)
    _write(_folder(b) / "options" / "settingsSync.xml", _fixture("settingsSync-off.xml"))

    # when
    report = _run(b)

    # then
    assert [d for d in report.deferred if d.reason == SYNC_CONFLICT] == []
    assert (_folder(b) / "options" / "editor-font.xml").read_bytes() == _fixture("editor-font.xml")


# --------------------------------------------------------------------------- an app that is absent or open


def test_an_app_that_is_not_installed_defers_the_apply_and_writes_nothing(tmp_path, memory_key_store) -> None:
    # given — B's Spotlight finds no DataGrip
    _, b = _pair(tmp_path, memory_key_store)

    # when
    report = _run(b, locate=())

    # then — a deferral, no file, and no records lost
    assert [d for d in report.deferred if d.path == EDITOR_FONT and "not installed" in d.reason] != []
    assert list(_folder(b).iterdir()) == []


@pytest.mark.parametrize("answer", [True, None])
def test_an_open_or_unknowable_app_defers_the_apply_and_collect_proceeds(
    tmp_path, memory_key_store, answer: bool | None
) -> None:
    # given — A edits a file; B (app open) edits a scheme of its own
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Later"))
    _run(a)
    _write(_folder(b) / "colors" / "sample.icls", _fixture("sample.icls").replace(b"ABCDEF", b"654321"))

    # when
    report = _run(b, running=FakeRunning(answer))

    # then — the apply waits, and the local edit was still published
    assert [d for d in report.deferred if d.path == KEYMAP and "quit it" in d.reason] != []
    assert b"Later" not in (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes()
    _run(a)
    assert b"654321" in (_folder(a) / "colors" / "sample.icls").read_bytes()


def test_a_deferred_apply_lands_once_the_app_has_quit(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Later"))
    _run(a)
    _run(b, running=FakeRunning(True))

    # when
    report = _run(b, running=FakeRunning(False))

    # then
    assert KEYMAP in report.applied
    assert b"Later" in (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes()


# --------------------------------------------------------------------------- unreadable or missing Settings Sync

UNREADABLE = "could not read settingsSync.xml; leaving files alone"


def _make_symlink(machine: Machine) -> None:
    target = machine.home / "elsewhere.xml"
    target.write_bytes(_fixture("settingsSync-off.xml"))
    (_folder(machine) / "options").mkdir(parents=True, exist_ok=True)
    (_folder(machine) / "options" / "settingsSync.xml").symlink_to(target)


def _make_oversize(machine: Machine) -> None:
    padding = b"<!-- " + b"x" * (64 * 1024) + b" -->"
    _write(_folder(machine) / "options" / "settingsSync.xml", _fixture("settingsSync-off.xml") + padding)


@pytest.mark.parametrize("make_unreadable", [_make_symlink, _make_oversize], ids=["symlink", "oversize"])
def test_an_unreadable_settings_sync_file_leaves_the_product_alone_and_says_why(
    tmp_path, memory_key_store, make_unreadable
) -> None:
    # given — B's settingsSync.xml cannot be read safely; B edits a file and A publishes a different edit
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    make_unreadable(b)
    local = _fixture("editor-font.xml").replace(b'"13"', b'"30"')
    _write(_folder(b) / "options" / "editor-font.xml", local)
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Remote"))
    _run(a)
    before = _records(a)
    published = GitCLITransport(a.repo).head()

    # when
    report = _run(b)

    # then — the product is unknown: not collected, not applied, reported, the live files untouched, nothing committed
    assert {d.reason for d in report.deferred if d.path in (EDITOR_FONT, KEYMAP)} == {UNREADABLE}
    assert (_folder(b) / "options" / "editor-font.xml").read_bytes() == local
    assert b"Remote" not in (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes()
    assert _records(b) == before
    assert [o for o in _objects(b) if b'"30"' in o] == []
    assert GitCLITransport(b.repo).head() == published


def test_a_missing_settings_sync_file_lets_the_flow_proceed(tmp_path, memory_key_store) -> None:
    # given — B has no settingsSync.xml at all, and A publishes an edit
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Remote"))
    _run(a)

    # when
    report = _run(b)

    # then
    assert (_folder(b) / "options" / "settingsSync.xml").exists() is False
    assert [d for d in report.deferred if d.reason in (UNREADABLE, SYNC_CONFLICT)] == []
    assert KEYMAP in report.applied
    assert b"Remote" in (_folder(b) / "options" / "mac" / "keymap.xml").read_bytes()
