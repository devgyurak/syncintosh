# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the VS Code preset — machine-local keys, a proxy credential, and keybindings.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
VS Code is not installed on the machine this was written on, so the fixtures are shaped from the
source research recorded in ``presets/vscode.py`` rather than copied from a real profile
(decision V4). The catalog is the one the CLI wires.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import SecretScanError
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.vscode import KEYBINDINGS, SETTINGS
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.sync import SyncReport, sync
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
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jsonc"


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
    recovery: Path | None = None
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


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


def _config(machine: Machine) -> SyncConfig:
    return SyncConfig.create(
        remote=machine.remote,
        machine_id=machine.machine_id,
        items=(
            LocalItem.create(path=SETTINGS, preset="vscode", scope="common", format="jsonc"),
            LocalItem.create(path=KEYBINDINGS, preset="vscode", scope="common", format="jsonc"),
        ),
    )


def _initialize(machine: Machine, *, import_from: Path | None = None) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    save_config(_config(machine), machine.config_file)
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


def _run(machine: Machine, *, adopt: AdoptChoice | None = None) -> SyncReport:
    active = GitCLITransport(machine.repo)
    return sync(
        transport=active,
        files=FileRepo(machine.repo),
        cipher=EnvelopeCipher(machine.store),
        scanner=PatternSecretScanner(),
        store=machine.store,
        codec=machine.codec,
        driver=TextMergeDriver(),
        adapters={"jsonc": JsoncAdapter()},
        catalog=BuiltinCatalog.default(),
        live=SystemLiveFiles(),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        adopt=adopt,
    )


def _write(machine: Machine, relpath: str, data: bytes) -> None:
    target = machine.home / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _read(machine: Machine, relpath: str) -> bytes:
    return (machine.home / relpath).read_bytes()


def _decrypted_objects(machine: Machine) -> list[bytes]:
    """Every object the machine's repo holds, decrypted — the check a leak cannot pass."""
    cipher = EnvelopeCipher(machine.store)
    files = FileRepo(machine.repo)
    return [
        cipher.open(files.read(rel) or b"", domain="object", file_id=Path(rel).stem)
        for rel in files.list_paths("objects")
    ]


def _pair(tmp_path: Path, make_store, settings: bytes) -> tuple[Machine, Machine]:
    """Two machines in sync on `settings`, both tracking the two VS Code files."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, settings)
    _write(a, KEYBINDINGS, _fixture("vscode-keybindings.jsonc"))
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    # The app directory exists, so the apply is not deferred — B simply has no file yet.
    (b.home / "Library" / "Application Support" / "Code" / "User").mkdir(parents=True)
    _run(b)
    return a, b


# --------------------------------------------------------------------------- machine-local keys


def test_a_machine_local_key_only_change_produces_no_commit(tmp_path, memory_key_store) -> None:
    # given — two machines in sync on the settings fixture
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    before = _git(a.repo, "rev-list", "--count", "HEAD").strip()

    # when — only a key VS Code does not sync changes
    _write(a, SETTINGS, _read(a, SETTINGS).replace(b'"window.zoomLevel": 1', b'"window.zoomLevel": 3'))
    report = _run(a)

    # then — no diff, no commit, no conflict, and the local value is left alone
    assert report.unchanged is True
    assert _git(a.repo, "rev-list", "--count", "HEAD").strip() == before
    assert b'"window.zoomLevel": 3' in _read(a, SETTINGS)
    assert b'"window.zoomLevel"' not in _read(b, SETTINGS)


def test_another_machines_machine_local_value_is_never_overwritten(tmp_path, memory_key_store) -> None:
    # given — A published its settings, and B has VS Code with its own machine-local values
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, _fixture("vscode.jsonc"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write(b, SETTINGS, _fixture("vscode.jsonc").replace(b'"/usr/local/bin/git"', b'"/opt/homebrew/bin/git"'))

    # when — B takes the repo's side, then A changes a shared setting and B syncs
    _run(b, adopt="remote")
    _write(a, SETTINGS, _read(a, SETTINGS).replace(b'"editor.tabSize": 4', b'"editor.tabSize": 2'))
    _run(a)
    report = _run(b)

    # then — A's setting landed and B's own path survived both applies
    assert report.has_conflicts is False
    merged = _read(b, SETTINGS)
    assert b'"editor.tabSize": 2' in merged
    assert b'"/opt/homebrew/bin/git"' in merged
    assert b'"/usr/local/bin/git"' not in merged


def test_a_machine_that_never_had_the_file_gets_the_file_without_the_stripped_keys(tmp_path, memory_key_store) -> None:
    # given — B has VS Code installed but has never opened Settings
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))

    # when
    merged = _read(b, SETTINGS)

    # then — the shared settings arrive and the machine-local ones are left to VS Code's defaults,
    # which is what "this key is not synced" means for a machine that never set it
    assert b'"editor.tabSize": 4' in merged
    assert b"window.zoomLevel" not in merged
    assert b"git.path" not in merged
    assert b"proxyAuthorization" not in merged
    assert b"// Editor settings for this machine." in merged
    assert a.repo.exists() is True


# --------------------------------------------------------------------------- the credential


def test_a_proxy_credential_never_reaches_a_repo_object(tmp_path, memory_key_store) -> None:
    # given — the settings fixture carries `http.proxyAuthorization`
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))

    # when
    plaintexts = _decrypted_objects(a)

    # then — the dotted key was cut before sealing, and the file keeps it
    assert [blob for blob in plaintexts if b"PROXY-AUTH-MARKER" in blob] == []
    assert [blob for blob in plaintexts if b"proxyAuthorization" in blob] == []
    assert b"PROXY-AUTH-MARKER" in _read(a, SETTINGS)


def test_a_nested_spelling_of_the_proxy_credential_is_also_cut(tmp_path, memory_key_store) -> None:
    # given — the same setting written as a nested object instead of a dotted key
    nested = (
        b'{\n  "editor.tabSize": 4,\n  "http": {\n    "proxyAuthorization": "Basic NESTED-PROXY-AUTH-MARKER"\n  }\n}\n'
    )
    a, _ = _pair(tmp_path, memory_key_store, nested)

    # when
    plaintexts = _decrypted_objects(a)

    # then — both spellings are registered, so neither can carry a credential in
    assert [blob for blob in plaintexts if b"NESTED-PROXY-AUTH-MARKER" in blob] == []
    assert b"NESTED-PROXY-AUTH-MARKER" in _read(a, SETTINGS)


def test_this_machines_proxy_credential_survives_an_apply(tmp_path, memory_key_store) -> None:
    # given — A published its settings; B's own file holds a different proxy credential
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, b'{\n  "editor.tabSize": 2,\n  "http.proxyAuthorization": "Basic FROM-A"\n}\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write(b, SETTINGS, b'{\n  "editor.tabSize": 4,\n  "http.proxyAuthorization": "Basic FROM-B"\n}\n')

    # when
    _run(b, adopt="remote")

    # then — A's setting landed and B's own credential is untouched
    merged = _read(b, SETTINGS)
    assert b'"editor.tabSize": 2' in merged
    assert b"Basic FROM-B" in merged
    assert b"Basic FROM-A" not in merged


def test_a_proxy_url_with_a_password_is_refused_by_the_secret_scan(tmp_path, memory_key_store) -> None:
    # given — a URL with inline credentials under a key the preset does not strip
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine)
    _write(machine, SETTINGS, b'{\n  "http.proxy": "http://builder:PROXY-PASSWORD@proxy.example.test:3128"\n}\n')

    # when
    with pytest.raises(SecretScanError) as refusal:
        _run(machine)

    # then — refused at the encrypt boundary, naming the location only (§3.4, §4)
    assert SETTINGS in str(refusal.value)
    assert "PROXY-PASSWORD" not in str(refusal.value)
    assert FileRepo(machine.repo).list_paths("objects") == ()


# --------------------------------------------------------------------------- keybindings


def test_a_both_sides_keybindings_change_is_a_whole_file_conflict(tmp_path, memory_key_store) -> None:
    # given — the array is compared whole, because its order carries meaning
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    original = _fixture("vscode-keybindings.jsonc")
    _write(a, KEYBINDINGS, original.replace(b"terminal.clear", b"terminal.clearAll"))
    _run(a)
    _write(b, KEYBINDINGS, original.replace(b"terminal.clear", b"terminal.kill"))

    # when
    report = _run(b)

    # then — one conflict for the document, and B's file is untouched
    assert report.has_conflicts is True
    assert report.conflicts[0].path == KEYBINDINGS
    assert report.conflicts[0].detail == "the same region changed on both sides: (document)"
    assert _read(b, KEYBINDINGS) == original.replace(b"terminal.clear", b"terminal.kill")


def test_a_one_sided_keybindings_change_keeps_the_order(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    edited = _fixture("vscode-keybindings.jsonc").replace(
        b'"command": "workbench.action.terminal.clear"', b'"command": "workbench.action.terminal.clearAll"'
    )

    # when — A edits the first binding and B syncs
    _write(a, KEYBINDINGS, edited)
    _run(a)
    _run(b)

    # then — the array arrived verbatim, order and `-command` removal both intact
    assert _read(b, KEYBINDINGS) == edited


# --------------------------------------------------------------------------- formats and edges


def test_a_tab_indented_crlf_settings_file_round_trips(tmp_path, memory_key_store) -> None:
    # given — a settings file with CRLF line endings, tabs, and a trailing comma
    original = _fixture("vscode-tabs-crlf.jsonc")
    a, b = _pair(tmp_path, memory_key_store, original)

    # when — A changes one value
    _write(a, SETTINGS, original.replace(b'"editor.fontSize": 13', b'"editor.fontSize": 15'))
    _run(a)
    _run(b)

    # then — only that value moved; the style survived on both machines
    assert _read(b, SETTINGS) == original.replace(b'"editor.fontSize": 13', b'"editor.fontSize": 15')
    assert b"\r\n" in _read(b, SETTINGS)


def test_a_machine_without_vscode_installed_defers(tmp_path, memory_key_store) -> None:
    # given — A published, and B has no VS Code directory at all
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    b = _machine(tmp_path, memory_key_store, "machine-c", a.remote)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b)

    # then — reported, and no directory tree was created
    assert sorted(report.deferred) == sorted((SETTINGS, KEYBINDINGS))
    assert (b.home / "Library").exists() is False


def test_a_credential_url_inside_a_keybinding_is_refused(tmp_path, memory_key_store) -> None:
    # given — a binding whose argument carries a URL with credentials. The file has no strip list
    # (an array is compared whole), so the secret scan is the only gate for it.
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    _write(
        a,
        KEYBINDINGS,
        b'[\n  {\n    "key": "cmd+t",\n    "command": "workbench.action.open",\n'
        b'    "args": {\n      "url": "http://user:binding-password@example.test"\n    }\n  }\n]\n',
    )

    # when & then — refused at the encrypt boundary, naming the file and never the value
    with pytest.raises(SecretScanError) as refusal:
        _run(a)
    assert KEYBINDINGS in str(refusal.value)
    assert "binding-password" not in str(refusal.value)


def test_a_conflict_on_an_unnamed_key_is_named_as_the_document(tmp_path, memory_key_store) -> None:
    # given — a key whose own name is empty, changed on both machines
    a, b = _pair(tmp_path, memory_key_store, b'{\n  "": 1\n}\n')
    _write(a, SETTINGS, b'{\n  "": 2\n}\n')
    _run(a)
    _write(b, SETTINGS, b'{\n  "": 3\n}\n')

    # when
    report = _run(b)

    # then — there is no key name to print, so the document is named rather than leaving a colon
    # dangling at the end of the detail
    assert report.conflicts[0].path == SETTINGS
    assert report.conflicts[0].detail == "the same region changed on both sides: (document)"
