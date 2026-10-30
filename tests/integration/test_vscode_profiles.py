# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: VS Code non-default profiles, matched by name across two Macs (issue #22).

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1). VS Code
is not installed on the machine this was written on, so the profile list is shaped from VS Code's own
source (`presets/vscode.py`): `storage.json` → `userDataProfiles` → `{name, location, useDefaultFlags}`.
The two Macs give the same profile different ids, which is the case the feature exists for.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.ports import PresetCatalog
from syncintosh.core.presets import EMPTY_CATALOG
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog, vscode
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.lock import FileLock
from syncintosh.system.pending import DirectoryPendingStore
from syncintosh.system.profile_index import SystemProfileIndex
from syncintosh.system.repo_files import FileRepo
from syncintosh.system.secret_scan import PatternSecretScanner
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
USER = "Library/Application Support/Code/User"
INDEX = f"{USER}/globalStorage/storage.json"

SETTINGS_TEXT = (
    b'{\n  "editor.tabSize": 4,\n  "window.zoomLevel": 2,\n  "http.proxyAuthorization": "Basic Zm9vOmJhcg=="\n}\n'
)
KEYBINDINGS_TEXT = b'[\n  { "key": "cmd+k cmd+s", "command": "workbench.action.openGlobalKeybindings" }\n]\n'
SNIPPET_TEXT = b'{\n  "log": { "prefix": "log", "body": ["console.log($1)"] }\n}\n'
PROFILE_FILES = {"settings.json": SETTINGS_TEXT, "keybindings.json": KEYBINDINGS_TEXT, "snippets/js.json": SNIPPET_TEXT}


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


def _initialize(machine: Machine, *, import_from: Path | None = None, extra: tuple[LocalItem, ...] = ()) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    base = new_machine_config(
        remote=machine.remote, machine_id=machine.machine_id, preset_names=("vscode",), catalog=BuiltinCatalog.default()
    )
    config = SyncConfig.create(
        remote=base.remote,
        machine_id=base.machine_id,
        items=(*base.items, *extra),
        globs=base.globs,
        profiles=base.profiles,
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


def _run(machine: Machine, *, adopt: AdoptChoice | None = None, catalog: PresetCatalog | None = None) -> SyncReport:
    active = GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
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
        catalog=BuiltinCatalog.default() if catalog is None else catalog,
        live=SystemLiveFiles(),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        apps=NO_APP_INFO,
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        adopt=adopt,
        profiles=SystemProfileIndex(str(machine.home), SystemDenyList(), vscode.read_profiles),
    )


def _write(machine: Machine, relpath: str, data: bytes) -> None:
    target = machine.home / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _register(machine: Machine, *profiles: dict[str, Any]) -> None:
    """Write the machine's own `storage.json` the way VS Code does (only the keys its source names)."""
    text = json.dumps({"userDataProfiles": list(profiles)})
    _write(machine, INDEX, text.encode("utf-8"))


def _profile_dir(machine: Machine, directory: str) -> Path:
    return machine.home / USER / "profiles" / directory


def _decrypted_objects(machine: Machine) -> list[bytes]:
    cipher = EnvelopeCipher(machine.store)
    files = FileRepo(machine.repo)
    return [
        cipher.open(files.read(rel) or b"", domain="object", file_id=Path(rel).stem)
        for rel in files.list_paths("objects")
    ]


def _pair(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """A has the profile "Work" under one id and has synced it; B is initialised with no profile list yet."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _register(a, {"name": "Work", "location": "-3f2a1b", "icon": "briefcase"})
    for relative, data in PROFILE_FILES.items():
        _write(a, f"{USER}/profiles/-3f2a1b/{relative}", data)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    return a, b


# --------------------------------------------------------------------------- (a) matched by name


def test_a_profile_syncs_by_name_when_the_two_macs_use_different_ids(tmp_path, memory_key_store) -> None:
    # given — B has its own "Work" profile under a different id and a folder for it
    _, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": "7c91e0aa"})
    _profile_dir(b, "7c91e0aa").mkdir(parents=True)

    # when
    report = _run(b)

    # then — settings, keybindings and the snippet land in B's own directory, without the stripped keys
    assert report.has_conflicts is False
    landed = {
        p.relative_to(_profile_dir(b, "7c91e0aa")).as_posix(): p.read_bytes()
        for p in _profile_dir(b, "7c91e0aa").rglob("*")
        if p.is_file()
    }
    assert sorted(landed) == ["keybindings.json", "settings.json", "snippets/js.json"]
    assert b'"editor.tabSize": 4' in landed["settings.json"]
    assert b"window.zoomLevel" not in landed["settings.json"]
    assert b"proxyAuthorization" not in landed["settings.json"]
    assert landed["keybindings.json"] == KEYBINDINGS_TEXT
    assert landed["snippets/js.json"] == SNIPPET_TEXT
    assert (_profile_dir(b, "-3f2a1b")).exists() is False


def test_noise_and_excluded_keys_never_reach_the_repo_and_no_name_or_id_is_in_a_filename(
    tmp_path, memory_key_store
) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)

    # when
    objects = _decrypted_objects(a)
    names = sorted(p.name for p in (a.repo / "objects").iterdir())

    # then — the credential and the machine-local key were cut before sealing, and the repo names nothing
    assert [o for o in objects if b"proxyAuthorization" in o or b"window.zoomLevel" in o] == []
    assert [n for n in names if "Work" in n or "3f2a1b" in n or not n.endswith(".enc")] == []


def test_a_change_on_one_mac_reaches_the_others_profile(tmp_path, memory_key_store) -> None:
    # given — both Macs have the profile and are in sync
    a, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": "7c91e0aa"})
    _profile_dir(b, "7c91e0aa").mkdir(parents=True)
    _run(b)

    # when — A edits a shared setting, and B has its own machine-local value
    _write(
        a,
        f"{USER}/profiles/-3f2a1b/settings.json",
        SETTINGS_TEXT.replace(b'"editor.tabSize": 4', b'"editor.tabSize": 2'),
    )
    _run(a)
    mine = _profile_dir(b, "7c91e0aa") / "settings.json"
    mine.write_bytes(
        mine.read_bytes().replace(b'"editor.tabSize": 4', b'"editor.tabSize": 4,\n  "window.zoomLevel": 5')
    )
    _run(b)

    # then — B received A's edit and kept its own zoom level
    text = mine.read_bytes()
    assert b'"editor.tabSize": 2' in text and b'"window.zoomLevel": 5' in text


# --------------------------------------------------------------------------- (b) a profile B lacks


def test_a_profile_this_mac_lacks_is_deferred_and_nothing_is_created_until_it_exists(
    tmp_path, memory_key_store
) -> None:
    # given — B has VS Code, a profile list, and no "Work" profile
    _, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Play", "location": "11aa22bb"})
    index_before = (b.home / INDEX).read_bytes()

    # when
    report = _run(b)

    # then — a non-blocking deferral says what to do; no profile directory appears; the index is untouched
    assert [d.reason for d in report.deferred if "Work" in d.reason] != []
    assert [r for r in (*report.refused, *report.unprojectable, *report.invalid) if r.blocking] == []
    assert sorted(p.name for p in (b.home / USER / "profiles").glob("*")) == []
    assert (b.home / INDEX).read_bytes() == index_before

    # when — the person creates the profile in the app, and B syncs again
    _register(b, {"name": "Play", "location": "11aa22bb"}, {"name": "Work", "location": "5d6e7f80"})
    _profile_dir(b, "5d6e7f80").mkdir(parents=True)
    _run(b)

    # then — it is applied on the next sync
    assert b'"editor.tabSize": 4' in (_profile_dir(b, "5d6e7f80") / "settings.json").read_bytes()


def test_a_registered_profile_without_a_folder_is_deferred_not_created(tmp_path, memory_key_store) -> None:
    # given — the app lists "Work" but has not written its folder
    _, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": "7c91e0aa"})

    # when
    report = _run(b)

    # then
    assert (_profile_dir(b, "7c91e0aa")).exists() is False
    assert [d.path for d in report.deferred if d.path.endswith("settings.json")] != []


# --------------------------------------------------------------------------- (c) a raw id path


def test_a_record_with_a_raw_id_path_is_never_written(tmp_path, memory_key_store) -> None:
    # given — a record that claims `vscode` for a real profile-id path. The shipped catalog never produces it,
    # so it is made by a publisher with no catalog (a hand-edited or hostile repo).
    remote = _bare_remote(tmp_path)
    raw = f"{USER}/profiles/-3f2a1b/settings.json"
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, extra=(LocalItem.create(path=raw, preset="vscode", scope="common", format="jsonc"),))
    _write(a, raw, SETTINGS_TEXT)
    _run(a, catalog=EMPTY_CATALOG)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _register(b, {"name": "Work", "location": "-3f2a1b"})
    _profile_dir(b, "-3f2a1b").mkdir(parents=True)

    # when
    report = _run(b)

    # then — it is refused as a path no preset owns, reported as a failure, and nothing is written there
    assert [r for r in report.refused if r.path == raw and r.blocking] != []
    assert (_profile_dir(b, "-3f2a1b") / "settings.json").exists() is False


# --------------------------------------------------------------------------- (d) borrowed resources


def test_a_resource_the_profile_borrows_from_the_default_profile_is_not_written(tmp_path, memory_key_store) -> None:
    # given — B's "Work" uses the default profile's settings, and has its own stale settings file
    _, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": "7c91e0aa", "useDefaultFlags": {"settings": True}})
    _write(b, f"{USER}/profiles/7c91e0aa/settings.json", b'{"mine": true}\n')

    # when
    report = _run(b)

    # then — the borrowed file is left alone and reported; the others still sync
    assert (_profile_dir(b, "7c91e0aa") / "settings.json").read_bytes() == b'{"mine": true}\n'
    assert [d for d in report.deferred if "default profile's settings" in d.reason] != []
    assert (_profile_dir(b, "7c91e0aa") / "keybindings.json").read_bytes() == KEYBINDINGS_TEXT


# --------------------------------------------------------------------------- (e) a broken index


@pytest.mark.parametrize("broken", [b"{", b'{"userDataProfiles": 3}'])
def test_a_broken_profile_list_resolves_nothing_reports_it_and_keeps_the_records(
    tmp_path, memory_key_store, broken: bytes
) -> None:
    # given — B once synced the profile; then its storage.json is damaged
    a, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": "7c91e0aa"})
    _profile_dir(b, "7c91e0aa").mkdir(parents=True)
    _run(b)
    mine = _profile_dir(b, "7c91e0aa") / "settings.json"
    kept = mine.read_bytes()
    _write(b, INDEX, broken)

    # when
    report = _run(b)

    # then — it is reported without blocking, the live file stays, and nothing is read as a deletion
    assert [r for r in report.refused if not r.blocking and r.path == vscode.PROFILE_INDEX] != []
    assert mine.read_bytes() == kept
    assert [e for e in JsonLedgerStore(b.ledger).load().values() if e.suppressed] == []

    # when — the list is repaired
    _register(b, {"name": "Work", "location": "7c91e0aa"})
    _write(
        a,
        f"{USER}/profiles/-3f2a1b/settings.json",
        SETTINGS_TEXT.replace(b'"editor.tabSize": 4', b'"editor.tabSize": 8'),
    )
    _run(a)
    _run(b)

    # then — the profile syncs again, as if nothing had happened
    assert b'"editor.tabSize": 8' in mine.read_bytes()


# --------------------------------------------------------------------------- (f) a symlinked directory


def test_a_symlinked_profile_directory_is_refused(tmp_path, memory_key_store) -> None:
    # given — B's profile directory is a symlink to somewhere else
    _, b = _pair(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": "7c91e0aa"})
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (b.home / USER / "profiles").mkdir(parents=True)
    _profile_dir(b, "7c91e0aa").symlink_to(outside)

    # when
    report = _run(b)

    # then — nothing is written through the link, and the profile is reported as skipped
    assert list(outside.iterdir()) == []
    assert [r for r in report.refused if "symbolic link" in r.reason] != []
