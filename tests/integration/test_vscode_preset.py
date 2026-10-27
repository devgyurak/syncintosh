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

from syncintosh.adapters.jsonc import UNRULED_REMOVAL_REFUSED, JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.advice import Advice
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import SyncConfig
from syncintosh.core.errors import SecretScanError
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.ports import AppDetector
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.core.resolution import Decide
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.vscode import KEYBINDINGS, SETTINGS, SNIPPETS_JSON
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.resolve import advised_chooser, decision, proposal_problem, take_chooser
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.lock import FileLock
from syncintosh.system.masking import TextMasker
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


class _StubApps:
    """A fixed `AppDetector` verdict, so the apply rule can be driven without `mdfind`."""

    def __init__(self, verdict: bool | None) -> None:
        self._verdict = verdict
        self.queried: list[str] = []

    def installed(self, bundle_id: str) -> bool | None:
        self.queried.append(bundle_id)
        return self._verdict


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
    # The shipped preset, so the config carries the two exact files and the two snippet globs — the
    # same thing `init` writes.
    return new_machine_config(
        remote=machine.remote,
        machine_id=machine.machine_id,
        preset_names=("vscode",),
        catalog=BuiltinCatalog.default(),
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


def _run(
    machine: Machine,
    *,
    adopt: AdoptChoice | None = None,
    apps: AppDetector | None = None,
    decide: Decide | None = None,
    resolving: bool = False,
) -> SyncReport:
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
        catalog=BuiltinCatalog.default(),
        live=SystemLiveFiles(),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        apps=NO_APP_INFO if apps is None else apps,
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        decide=decide,
        resolving=resolving,
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


def _with_binding(text: bytes, key: str, command: str) -> bytes:
    """The keybindings fixture with one binding appended to its array (the fixture ends `}\\n]`)."""
    return text.replace(b"\n]", f',\n  {{"key": "{key}", "command": "{command}"}}\n]'.encode())


def test_bindings_added_on_each_machine_are_both_kept(tmp_path, memory_key_store) -> None:
    # given — each machine adds a different binding, on a different key
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    _write(a, KEYBINDINGS, _with_binding(_fixture("vscode-keybindings.jsonc"), "cmd+k", "a.added"))
    _run(a)
    _write(b, KEYBINDINGS, _with_binding(_fixture("vscode-keybindings.jsonc"), "cmd+b", "b.added"))

    # when
    report = _run(b)

    # then — no conflict, and B's file holds both additions; A picks up the merge on its next sync
    assert report.has_conflicts is False
    assert b"a.added" in _read(b, KEYBINDINGS) and b"b.added" in _read(b, KEYBINDINGS)
    _run(a)
    assert _read(a, KEYBINDINGS) == _read(b, KEYBINDINGS)


def test_the_same_binding_changed_on_both_sides_is_a_conflict_on_that_binding_only(tmp_path, memory_key_store) -> None:
    # given — A and B change the first binding to different commands; B also adds an unrelated binding
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    original = _fixture("vscode-keybindings.jsonc")
    _write(a, KEYBINDINGS, original.replace(b"terminal.clear", b"terminal.clearAll"))
    _run(a)
    edited_b = _with_binding(original.replace(b"terminal.clear", b"terminal.kill"), "cmd+b", "b.added")
    _write(b, KEYBINDINGS, edited_b)

    # when
    report = _run(b)

    # then — the conflict names that binding; B's file is untouched until a pick settles it
    assert report.has_conflicts is True
    assert report.conflicts[0].path == KEYBINDINGS
    assert '[key="cmd+shift+k", when=(missing)]' in report.conflicts[0].detail
    assert _read(b, KEYBINDINGS) == edited_b

    # when — pick A's version of that one binding; B's own addition is kept
    _run(b, decide=decision(take_chooser("remote")), resolving=True)

    # then
    assert b"terminal.clearAll" in _read(b, KEYBINDINGS)
    assert b"terminal.kill" not in _read(b, KEYBINDINGS)
    assert b"b.added" in _read(b, KEYBINDINGS)


def test_a_removal_that_overlaps_an_edit_of_the_same_binding_conflicts(tmp_path, memory_key_store) -> None:
    # given — A removes the first binding; B edits that same binding
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    original = _fixture("vscode-keybindings.jsonc")
    removed = original.replace(
        b'  {\n    "key": "cmd+shift+k",\n    "command": "workbench.action.terminal.clear"\n  },\n', b""
    )
    assert removed != original
    _write(a, KEYBINDINGS, removed)
    _run(a)
    _write(b, KEYBINDINGS, original.replace(b"terminal.clear", b"terminal.kill"))

    # when
    report = _run(b)

    # then
    assert report.has_conflicts is True
    assert '[key="cmd+shift+k", when=(missing)]' in report.conflicts[0].detail


def test_a_non_object_element_on_one_side_falls_back_to_a_whole_array_conflict(tmp_path, memory_key_store) -> None:
    # given — A adds a string where a binding belongs, which the rule cannot identify; B edits a binding
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    original = _fixture("vscode-keybindings.jsonc")
    _write(a, KEYBINDINGS, original.replace(b"\n]", b',\n  "not a binding"\n]'))
    _run(a)
    _write(b, KEYBINDINGS, original.replace(b"terminal.clear", b"terminal.kill"))

    # when
    report = _run(b)

    # then — the array is one conflict, as it was before the rule existed
    assert report.has_conflicts is True
    assert report.conflicts[0].detail == "the same region changed on both sides: (document)"
    assert _read(b, KEYBINDINGS) == original.replace(b"terminal.clear", b"terminal.kill")


def test_a_credential_in_a_binding_is_refused_before_anything_is_published(tmp_path, memory_key_store) -> None:
    # given — B adds a binding whose args carry a credential-bearing URL
    _a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    leaked = _with_binding(_fixture("vscode-keybindings.jsonc"), "cmd+u", "x.open")
    leaked = leaked.replace(
        b'"command": "x.open"}',
        b'"command": "x.open", "args": {"url": "https://builder:PROXY-PASSWORD@proxy.example.test"}}',
    )
    _write(b, KEYBINDINGS, leaked)

    # when & then — refused at the encrypt boundary, naming the location only
    with pytest.raises(SecretScanError) as refusal:
        _run(b)
    assert KEYBINDINGS in str(refusal.value)
    assert "PROXY-PASSWORD" not in str(refusal.value)


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
    assert sorted(deferral.path for deferral in report.deferred) == sorted((SETTINGS, KEYBINDINGS))
    assert (b.home / "Library").exists() is False


def test_an_uninstalled_app_defers_with_the_vendors_download_page(tmp_path, memory_key_store) -> None:
    # given — A published, B has no VS Code, and the detector says the app is not installed
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    b = _machine(tmp_path, memory_key_store, "machine-uninstalled", a.remote)
    _initialize(b, import_from=a.recovery)
    apps = _StubApps(False)

    # when
    report = _run(b, apps=apps)

    # then — deferred with the built-in preset's own page and cask, and no tree created
    assert sorted(deferral.path for deferral in report.deferred) == sorted((SETTINGS, KEYBINDINGS))
    assert {deferral.reason for deferral in report.deferred} == {"the app is not installed on this machine"}
    assert {deferral.download_url for deferral in report.deferred} == {"https://code.visualstudio.com/download"}
    assert {deferral.cask for deferral in report.deferred} == {"visual-studio-code"}
    assert (b.home / "Library").exists() is False
    # one query for the whole preset, not one per file
    assert apps.queried == ["com.microsoft.VSCode"]


def test_a_missing_bundle_does_not_defer_when_the_directory_exists(tmp_path, memory_key_store) -> None:
    # given — B has VS Code's directory, but Spotlight reports the app missing (indexing off, or the
    # app under an excluded path): the directory is the stronger signal
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    b = _machine(tmp_path, memory_key_store, "machine-spotlight-blind", a.remote)
    _initialize(b, import_from=a.recovery)
    (b.home / "Library" / "Application Support" / "Code" / "User").mkdir(parents=True)

    # when
    report = _run(b, apps=_StubApps(False))

    # then — written, not deferred forever on a detector the user cannot fix
    assert report.deferred == ()
    assert sorted(report.applied) == sorted((SETTINGS, KEYBINDINGS))


def test_an_installed_app_creates_the_directory_and_the_file(tmp_path, memory_key_store) -> None:
    # given — the directory does not exist, but the detector says VS Code is installed
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    b = _machine(tmp_path, memory_key_store, "machine-installed", a.remote)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b, apps=_StubApps(True))

    # then — the documented change: the parent directory is created and the file is written
    assert report.deferred == ()
    assert sorted(report.applied) == sorted((SETTINGS, KEYBINDINGS))
    assert (b.home / "Library" / "Application Support" / "Code" / "User" / "settings.json").exists() is True


def test_an_undeterminable_app_falls_back_to_the_directory_heuristic(tmp_path, memory_key_store) -> None:
    # given — a detector that could not answer (no `mdfind`, or a timeout)
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    b = _machine(tmp_path, memory_key_store, "machine-unknown", a.remote)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b, apps=_StubApps(None))

    # then — the directory is absent, so the old behaviour holds: deferred, nothing created
    assert sorted(deferral.path for deferral in report.deferred) == sorted((SETTINGS, KEYBINDINGS))
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


def test_a_conflict_on_an_unnamed_key_is_not_confused_with_the_document(tmp_path, memory_key_store) -> None:
    # given — a key whose own name is empty, changed on both machines
    a, b = _pair(tmp_path, memory_key_store, b'{\n  "": 1\n}\n')
    _write(a, SETTINGS, b'{\n  "": 2\n}\n')
    _run(a)
    _write(b, SETTINGS, b'{\n  "": 3\n}\n')

    # when
    report = _run(b)

    # then — each step is quoted rather than joined bare, so an unnamed key reads `""` (and the
    # detail never trails a colon) instead of being confused with a whole-document conflict
    assert report.conflicts[0].path == SETTINGS
    assert report.conflicts[0].detail == 'the same region changed on both sides: ""'


# --------------------------------------------------------------------------- snippet globs


def test_a_snippet_from_another_machine_is_applied_under_the_glob(tmp_path, memory_key_store) -> None:
    # given — A has a language snippet; B has VS Code's directories but no snippet yet
    a, _ = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    snippet = SNIPPETS_JSON.replace("*", "javascript")
    _write(a, snippet, b'{\n  "log": {\n    "prefix": "log",\n    "body": "console.log($1)"\n  }\n}\n')
    _run(a)

    b = _machine(tmp_path, memory_key_store, "machine-snippet", a.remote)
    _initialize(b, import_from=a.recovery)
    (b.home / "Library" / "Application Support" / "Code" / "User" / "snippets").mkdir(parents=True)

    # when
    report = _run(b)

    # then — the record arrived, B's own config names the same glob, so it is written and settles
    assert snippet in report.applied
    assert _read(b, snippet) == _read(a, snippet)
    assert report.deferred == ()


class _ProposingAdvisor:
    """A model stand-in that answers one binding with a proposed array, and records what it was asked."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.asked: list[str] = []

    def advise(self, index: int, *, path, key, base, local, remote):
        self.asked.append(key.text)
        return Advice(choice="proposal", value=self.value, rationale="the merged command")


def test_a_model_proposal_for_one_binding_settles_only_that_binding(tmp_path, memory_key_store) -> None:
    # given — A and B change the same binding differently; B also adds an unrelated binding
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    original = _fixture("vscode-keybindings.jsonc")
    _write(a, KEYBINDINGS, original.replace(b"terminal.clear", b"terminal.clearAll"))
    _run(a)
    _write(b, KEYBINDINGS, _with_binding(original.replace(b"terminal.clear", b"terminal.kill"), "cmd+b", "b.added"))
    assert _run(b).has_conflicts is True

    # when — the model proposes one binding of that group; the person accepts it
    advisor = _ProposingAdvisor('[{"key": "cmd+shift+k", "command": "terminal.both"}]')
    catalog = BuiltinCatalog.default()
    chooser = advised_chooser(
        advisor=advisor,
        masker=TextMasker(home=str(b.home), username="builder", internal_domains=()),
        fallback=lambda view: "remote",
        present=lambda suggestion: True,
        auto_apply=lambda view: False,
        check=lambda view, text: proposal_problem(
            view, text, catalog=catalog, adapters={"jsonc": JsoncAdapter()}, validators={}
        ),
    )
    _run(b, decide=decision(chooser), resolving=True)

    # then — the binding is the model's; B's own addition survives; the model was told which binding
    assert advisor.asked and "JSON array" in advisor.asked[0] and 'key="cmd+shift+k"' in advisor.asked[0]
    assert b"terminal.both" in _read(b, KEYBINDINGS)
    assert b"b.added" in _read(b, KEYBINDINGS)
    assert b"terminal.clearAll" not in _read(b, KEYBINDINGS)
    _run(a)
    assert _read(a, KEYBINDINGS) == _read(b, KEYBINDINGS)


def test_a_reordered_binding_group_settles_in_one_resolve_and_both_machines_converge(
    tmp_path, memory_key_store
) -> None:
    # given — two bindings share the key `cmd+shift+k`; A reorders them and edits the first, B edits the second
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    base = _with_binding(_fixture("vscode-keybindings.jsonc"), "cmd+shift+k", "b.extra")
    _write(a, KEYBINDINGS, base)
    _run(a)
    _run(b)
    reordered_by_a = (
        b"// Place your key bindings in this file to override the defaults\n[\n"
        b'  {"key": "cmd+shift+k", "command": "b.extra"},\n'
        b'  {\n    "key": "cmd+shift+k",\n    "command": "workbench.action.terminal.clear",\n'
        b'    "args": {"a": 1}\n  },\n'
        b'  {"key": "cmd+alt+left", "command": "-workbench.action.navigateBack"}\n]'
    )
    _write(a, KEYBINDINGS, reordered_by_a)
    _run(a)
    edited_by_b = base.replace(b'"command": "b.extra"}', b'"command": "b.extra", "args": {"b": 2}}')
    _write(b, KEYBINDINGS, edited_by_b)

    # when — B's sync stops on the group; B resolves it by taking its own side, in one round
    stopped = _run(b)
    assert stopped.has_conflicts is True
    settled = _run(b, decide=decision(take_chooser("local")), resolving=True)

    # then — no conflict is left, B keeps its own version of the group, and A converges to it
    assert settled.has_conflicts is False
    assert b'"b": 2' in _read(b, KEYBINDINGS)
    assert _read(b, KEYBINDINGS).index(b"terminal.clear") < _read(b, KEYBINDINGS).index(b"b.extra")
    _run(a)
    assert _read(a, KEYBINDINGS) == _read(b, KEYBINDINGS)


def test_a_moved_binding_converges_on_the_other_machine_with_its_notes_above_it(tmp_path, memory_key_store) -> None:
    # given — two bindings with a note above each; A moves them, B edits the first one's args
    a, b = _pair(tmp_path, memory_key_store, _fixture("vscode.jsonc"))
    with_notes = (
        b'[\n  // note on clear\n  {"key": "cmd+shift+k", "command": "workbench.action.terminal.clear"},\n'
        b'  // note on navigate\n  {"key": "cmd+alt+left", "command": "-workbench.action.navigateBack"}\n]\n'
    )
    _write(a, KEYBINDINGS, with_notes)
    _run(a)
    _run(b)
    _write(
        a,
        KEYBINDINGS,
        with_notes.replace(
            b'"command": "workbench.action.terminal.clear"},', b'"command": "workbench.action.terminal.clear"},', 1
        ),
    )
    moved_by_a = (
        b'[\n  // note on navigate\n  {"key": "cmd+alt+left", "command": "-workbench.action.navigateBack"},\n'
        b'  // note on clear\n  {"key": "cmd+shift+k", "command": "workbench.action.terminal.clear"}\n]\n'
    )
    _write(a, KEYBINDINGS, moved_by_a)
    _run(a)
    _write(
        b,
        KEYBINDINGS,
        with_notes.replace(
            b'"command": "workbench.action.terminal.clear"}',
            b'"command": "workbench.action.terminal.clear", "args": {"x": 1}}',
        ),
    )

    # when
    report = _run(b)

    # then — no conflict; B has A's order, each note still directly above its binding, and B's own edit
    text = _read(b, KEYBINDINGS)
    assert report.has_conflicts is False
    assert b'// note on navigate\n  {"key": "cmd+alt+left"' in text
    assert b'// note on clear\n  {"key": "cmd+shift+k"' in text
    assert text.index(b"navigateBack") < text.index(b"terminal.clear")
    assert b'"args": {"x": 1}' in text
    _run(a)
    assert _read(a, KEYBINDINGS) == text


def test_a_removed_note_bearing_element_of_a_rule_less_array_stays_a_conflict_with_its_reason(
    tmp_path, memory_key_store
) -> None:
    # given — A removes the element of `editor.rulers` that carries a note; B edits an unrelated setting
    rulers = b'{\n  "editor.fontSize": 13,\n  "editor.rulers": [\n    80, // pep8\n    120\n  ]\n}\n'
    a, b = _pair(tmp_path, memory_key_store, rulers)
    _write(a, SETTINGS, b'{\n  "editor.fontSize": 13,\n  "editor.rulers": [\n    120\n  ]\n}\n')
    _run(a)
    _write(b, SETTINGS, rulers.replace(b"13", b"14"))

    # when
    report = _run(b)

    # then — a conflict, and the adapter's fixed reason is said; nothing is published over B's file
    assert report.has_conflicts is True
    assert UNRULED_REMOVAL_REFUSED in report.conflicts[0].detail
    assert b"pep8" in _read(b, SETTINGS)
