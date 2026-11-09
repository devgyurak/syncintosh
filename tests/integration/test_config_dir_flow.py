# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a versioned config directory, recorded by logical path, across two Macs on two releases (issue #54).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The preset is a TEST-ONLY
one (`widget`, a made-up product under the JetBrains vendor folder): the real JetBrains presets are a later change. The
installed app is a temp `.app` bundle whose `Contents/Resources/product-info.json` carries the shape a real one has
(`dataDirectoryName`, `productVendor`), found through a fake locator, and the running-app port is a fake.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.config_dirs import ConfigDirSpec
from syncintosh.core.errors import PolicyMismatch
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.policy import policy_digest
from syncintosh.core.ports import PresetCatalog
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
from syncintosh.system.app_config_dir import ProductInfoConfigDir
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
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
HOME = "Library/Application Support/JetBrains"
BUNDLE = "com.example.widget"
SPEC = ConfigDirSpec.create(home=HOME, prefix="Widget", vendor="JetBrains", bundle_id=BUNDLE)
WIDGET = Preset.create(
    name="widget",
    files=(
        PresetFile.create(path=SPEC.logical_path("options/editor.xml"), format="text"),
        PresetFile.create(path=SPEC.logical_path("options/security.xml"), format="text"),
        PresetFile.create(path=SPEC.logical_path("colors/*.xml"), format="text"),
    ),
    bundle_id=BUNDLE,
    config_dir=SPEC,
    defer_while_running=True,
)


@dataclass(frozen=True)
class _TestCatalog(BuiltinCatalog):
    """The shipped catalog's lookup over the test preset, with the one-entry policy history a shipped preset has."""

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        preset = self.get(name)
        return None if preset is None else (policy_digest(preset),)


CATALOG = _TestCatalog.create((WIDGET,))
EDITOR = SPEC.logical_path("options/editor.xml")
COLORS = SPEC.logical_path("colors/dark.xml")

EDITOR_TEXT = b'<application><component name="EditorFont" size="14"/></application>\n'
DARK_TEXT = b'<scheme name="Dark" parent="Darcula"/>\n'
SECRET_TEXT = b'<application><component name="PasswordSafe" master="do-not-sync"/></application>\n'


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
    """A `RunningApps` that answers what it is told and remembers each question."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def running(self, bundle_id: str) -> bool | None:
        self.asked.append(bundle_id)
        return self.answer


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _install_app(root: Path, directory: object, *, vendor: object = "JetBrains") -> Path:
    """A temp `.app` whose own metadata names `directory`, the way a real `product-info.json` does."""
    app = root / "Widget.app"
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    document = {"name": "Widget", "dataDirectoryName": directory, "productVendor": vendor}
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


def _initialize(
    machine: Machine, *, import_from: Path | None = None, catalog: PresetCatalog = CATALOG, track: bool = True
) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    config = new_machine_config(
        remote=machine.remote,
        machine_id=machine.machine_id,
        preset_names=("widget",) if track else (),
        catalog=catalog,
    )
    save_config(config, machine.config_file)
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


def _run(
    machine: Machine,
    *,
    running: FakeRunning | None = None,
    catalog: PresetCatalog = CATALOG,
    locate: tuple[str, ...] | None = None,
) -> SyncReport:
    active = GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    found = (str(machine.app),) if locate is None else locate
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
        catalog=catalog,
        live=SystemLiveFiles(),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        apps=NO_APP_INFO,
        running=FakeRunning(False) if running is None else running,
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        config_dirs=ProductInfoConfigDir(lambda _bundle: found),
    )


def _folder(machine: Machine, release: str | None = None) -> Path:
    return machine.home / HOME / (release or machine.release)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _manifest(machine: Machine):
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    return loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))


def _records(machine: Machine) -> dict[str, str]:
    return {record.path: record.file_id for record in _manifest(machine).records.values()}


def _pair(tmp_path: Path, make_store, *, a_release: str = "Widget2026.2", b_release: str = "Widget2026.3"):
    """A is on one release and has synced; B is on another, initialised, with its own (empty) folder."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote, a_release)
    _initialize(a)
    _write(_folder(a) / "options" / "editor.xml", EDITOR_TEXT)
    _write(_folder(a) / "colors" / "dark.xml", DARK_TEXT)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote, b_release)
    _initialize(b, import_from=a.recovery)
    _folder(b).mkdir(parents=True)
    return a, b


# --------------------------------------------------------------------------- two releases, one record


def test_two_macs_on_different_releases_converge_on_identical_records(tmp_path, memory_key_store) -> None:
    # given — A on 2026.2 has published; B on 2026.3 has its own folder
    a, b = _pair(tmp_path, memory_key_store)
    published = _records(a)

    # when
    report = _run(b)

    # then — the files land in B's own release folder, B's records are A's, and B never touched A's release name
    assert report.has_conflicts is False
    assert (_folder(b) / "options" / "editor.xml").read_bytes() == EDITOR_TEXT
    assert (_folder(b) / "colors" / "dark.xml").read_bytes() == DARK_TEXT
    assert (b.home / HOME / "Widget2026.2").exists() is False
    assert _records(b) == published
    assert sorted(published) == [COLORS, EDITOR]


def test_no_record_path_or_filename_names_a_release_folder(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)

    # when
    names = sorted(p.name for p in (a.repo / "objects").iterdir())

    # then — the version lives nowhere in the repo's names, and every path is the logical one
    assert [n for n in names if "2026" in n or "Widget" in n] == []
    assert [path for path in _records(a) if "2026" in path] == []


def test_a_change_on_either_release_reaches_the_other(tmp_path, memory_key_store) -> None:
    # given — both in sync
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)

    # when — B (2026.3) edits a file, A (2026.2) syncs; then A edits a glob file and B syncs
    _write(_folder(b) / "options" / "editor.xml", EDITOR_TEXT.replace(b'"14"', b'"16"'))
    _run(b)
    _run(a)
    _write(_folder(a) / "colors" / "dark.xml", DARK_TEXT.replace(b"Darcula", b"IntelliJ"))
    _run(a)
    _run(b)

    # then
    assert b'size="16"' in (_folder(a) / "options" / "editor.xml").read_bytes()
    assert b"IntelliJ" in (_folder(b) / "colors" / "dark.xml").read_bytes()


def test_a_glob_inside_the_versioned_directory_is_listed_where_the_app_really_keeps_it(
    tmp_path, memory_key_store
) -> None:
    # given — a stale folder of another release holds a colour scheme the resolved one does not
    a, b = _pair(tmp_path, memory_key_store)
    _write(_folder(b, "Widget2025.9") / "colors" / "stale.xml", b"<scheme/>\n")
    _write(_folder(b) / "colors" / "mine.xml", b'<scheme name="Mine"/>\n')

    # when
    _run(b)

    # then — only the resolved folder was listed
    assert sorted(_records(b)) == sorted([*_records(a), SPEC.logical_path("colors/mine.xml")])


# --------------------------------------------------------------------------- a folder this Mac lacks


def test_a_missing_release_folder_defers_and_is_never_created(tmp_path, memory_key_store) -> None:
    # given — the app is installed, but its first run has not made the folder
    _, b = _pair(tmp_path, memory_key_store)
    shutil.rmtree(b.home / "Library")

    # when
    report = _run(b)

    # then — a non-blocking deferral, and not even the vendor directory appears
    assert [d for d in report.deferred if d.path == EDITOR and "settings folder" in d.reason] != []
    assert [r for r in (*report.refused, *report.unprojectable, *report.invalid) if r.blocking] == []
    assert (b.home / "Library" / "Application Support" / "JetBrains").exists() is False

    # when — the app runs once and makes the folder
    _folder(b).mkdir(parents=True)
    _run(b)

    # then — it applies on the next sync
    assert (_folder(b) / "options" / "editor.xml").read_bytes() == EDITOR_TEXT


# --------------------------------------------------------------------------- metadata that cannot be trusted


@pytest.mark.parametrize(
    "metadata",
    [
        "{",
        json.dumps({"dataDirectoryName": "Other2026.3", "productVendor": "JetBrains"}),
        json.dumps({"dataDirectoryName": "Widget2026.3/../../x", "productVendor": "JetBrains"}),
        json.dumps({"dataDirectoryName": "../Widget2026.3", "productVendor": "JetBrains"}),
        json.dumps({"dataDirectoryName": "Widget2026.3/sub", "productVendor": "JetBrains"}),
        json.dumps({"dataDirectoryName": "Widget2026.3", "productVendor": "Evil Inc"}),
    ],
)
def test_untrusted_metadata_resolves_nothing_and_the_records_are_kept(
    tmp_path, memory_key_store, metadata: str
) -> None:
    # given — B synced fine once; then its app's metadata becomes untrustworthy
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    kept = (_folder(b) / "options" / "editor.xml").read_bytes()
    (b.app / "Contents" / "Resources" / "product-info.json").write_text(metadata, encoding="utf-8")
    _write(_folder(a) / "options" / "editor.xml", EDITOR_TEXT.replace(b'"14"', b'"18"'))
    _run(a)
    before = _records(a)

    # when
    report = _run(b)

    # then — deferred, the live file and the ledger are untouched, and the record is not read as a deletion
    assert [d for d in report.deferred if d.path == EDITOR] != []
    assert (_folder(b) / "options" / "editor.xml").read_bytes() == kept
    assert [e for e in JsonLedgerStore(b.ledger).load().values() if e.suppressed] == []
    assert _records(b) == before
    assert sorted(p.name for p in (b.home / HOME).iterdir()) == [b.release]


def test_with_several_installs_the_highest_release_is_used(tmp_path, memory_key_store) -> None:
    # given — two installs of the app (2026.2 and 2026.3), and both folders exist on B
    _, b = _pair(tmp_path, memory_key_store)
    older = _install_app(tmp_path / "older", "Widget2026.2")
    _folder(b, "Widget2026.2").mkdir(parents=True)

    # when
    _run(b, locate=(str(older), str(b.app)))

    # then — the higher one gets the files, and the other release's folder is left empty
    assert (_folder(b) / "options" / "editor.xml").read_bytes() == EDITOR_TEXT
    assert list(_folder(b, "Widget2026.2").iterdir()) == []


# --------------------------------------------------------------------------- an app that is open


@pytest.mark.parametrize("answer", [True, None])
def test_an_open_or_unknowable_app_defers_the_apply_and_collect_proceeds(
    tmp_path, memory_key_store, answer: bool | None
) -> None:
    # given — both in sync; A edits the editor file, and B (app open) edits a colour scheme of its own
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    _write(_folder(a) / "options" / "editor.xml", EDITOR_TEXT.replace(b'"14"', b'"20"'))
    _run(a)
    _write(_folder(b) / "colors" / "dark.xml", DARK_TEXT.replace(b"Darcula", b"Mine"))
    running = FakeRunning(answer)

    # when
    report = _run(b, running=running)

    # then — the apply waits (one question for the cycle), the local edit was still collected and published
    assert [d for d in report.deferred if d.path == EDITOR and "quit it" in d.reason] != []
    assert (_folder(b) / "options" / "editor.xml").read_bytes() == EDITOR_TEXT
    assert running.asked == [BUNDLE]
    _run(a)
    assert b"Mine" in (_folder(a) / "colors" / "dark.xml").read_bytes()


def test_a_deferred_apply_is_retried_on_the_next_sync_once_the_app_has_quit(tmp_path, memory_key_store) -> None:
    # given — an apply held back while the app was open
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)
    _write(_folder(a) / "options" / "editor.xml", EDITOR_TEXT.replace(b'"14"', b'"22"'))
    _run(a)
    _run(b, running=FakeRunning(True))

    # when — the app has quit
    report = _run(b, running=FakeRunning(False))

    # then
    assert [path for path in report.applied if path == EDITOR] == [EDITOR]
    assert b'size="22"' in (_folder(b) / "options" / "editor.xml").read_bytes()


def test_an_app_that_is_open_does_not_block_a_file_that_would_not_change(tmp_path, memory_key_store) -> None:
    # given — B is already in sync
    _, b = _pair(tmp_path, memory_key_store)
    _run(b)

    # when
    report = _run(b, running=FakeRunning(True))

    # then — nothing to apply, so nothing to wait for
    assert [d for d in report.deferred if "quit it" in d.reason] == []


# --------------------------------------------------------------------------- an older build / protected paths


def test_a_build_without_the_preset_refuses_the_repo_and_writes_nothing(tmp_path, memory_key_store) -> None:
    # given — C runs a build whose catalog has no such preset (an older build) and tracks nothing of it
    a, _ = _pair(tmp_path, memory_key_store)
    c = _machine(tmp_path, memory_key_store, "machine-c", a.remote, "Widget2026.3")
    _initialize(c, import_from=a.recovery, catalog=BuiltinCatalog.default(), track=False)

    # when & then — the policy gate stops the cycle (a preset this build never shipped), before any path is read
    with pytest.raises(PolicyMismatch):
        _run(c, catalog=BuiltinCatalog.default())
    assert (c.home / "Library").exists() is False


def test_an_old_catalog_that_never_heard_of_the_preset_owns_no_such_path() -> None:
    # given
    older = BuiltinCatalog.default()

    # when
    owner, policy = older.preset_for(EDITOR), older.policy_for(EDITOR)

    # then — no preset owns the logical path, so nothing here would resolve, collect or project it
    assert (owner, policy) == (None, None)


def test_a_protected_file_of_the_preset_is_never_collected(tmp_path, memory_key_store) -> None:
    # given — the preset lists `options/security.xml`, which the deny-list protects in every release folder
    a, _ = _pair(tmp_path, memory_key_store)
    _write(_folder(a) / "options" / "security.xml", SECRET_TEXT)

    # when
    report = _run(a)

    # then — skipped by the deny-list, never a record, and its content is in no object
    assert SPEC.logical_path("options/security.xml") in report.skipped
    assert [p for p in _records(a) if "security" in p] == []
    cipher = EnvelopeCipher(a.store)
    files = FileRepo(a.repo)
    objects = [
        cipher.open(files.read(rel) or b"", domain="object", file_id=Path(rel).stem)
        for rel in files.list_paths("objects")
    ]
    assert [o for o in objects if b"do-not-sync" in o] == []


def test_a_symlinked_release_folder_is_refused(tmp_path, memory_key_store) -> None:
    # given — B's release folder is a symlink to somewhere else
    _, b = _pair(tmp_path, memory_key_store)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _folder(b).rmdir()
    _folder(b).symlink_to(outside)

    # when
    report = _run(b)

    # then — nothing is written through the link
    assert list(outside.iterdir()) == []
    assert [r for r in report.refused if "symbolic link" in r.reason] != []


def test_an_explicit_item_at_a_real_release_path_is_refused_as_unowned(tmp_path, memory_key_store) -> None:
    # given — a hand-edited config tracks a real release path, which only the logical path may stand for
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote, "Widget2026.2")
    _initialize(a)
    real = f"{HOME}/Widget2026.2/options/editor.xml"
    config = load_config(a.config_file)
    save_config(
        SyncConfig.create(
            remote=config.remote,
            machine_id=config.machine_id,
            items=(*config.items, LocalItem.create(path=real, preset="widget", scope="common", format="text")),
            globs=config.globs,
        ),
        a.config_file,
    )
    _write(_folder(a) / "options" / "editor.xml", EDITOR_TEXT)

    # when
    report = _run(a)

    # then — the real spelling is not collected as a second record for the same file
    assert [r for r in report.refused if r.path == real] != []
    assert real not in _records(a)


def test_a_symlinked_middle_directory_is_refused(tmp_path, memory_key_store) -> None:
    # given — `colors/` inside B's release folder is a symlink to another place under the same home
    _, b = _pair(tmp_path, memory_key_store)
    elsewhere = b.home / "elsewhere"
    elsewhere.mkdir()
    (_folder(b) / "colors").symlink_to(elsewhere)

    # when
    _run(b)

    # then — nothing is written through the link
    assert list(elsewhere.iterdir()) == []
