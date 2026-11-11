# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the Zed preset — credentials at every override layer, machine-local keys, keymap and snippets.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
Zed is not installed on the machine this was written on, so the fixtures are shaped from the
source research recorded in ``presets/zed.py`` rather than copied from a real profile (the approved
source-verified exception). The catalog is the one the CLI wires.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _harness import run_sync

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import SyncConfig
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.ports import AppDetector
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.core.resolution import Decide
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.zed import KEYMAP, SETTINGS, SNIPPETS
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport
from syncintosh.system.config import save_config
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.repo_files import FileRepo
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zed"


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
    # The shipped preset, so the config carries the two exact files and the two globs — the
    # same thing `init` writes.
    return new_machine_config(
        remote=machine.remote,
        machine_id=machine.machine_id,
        preset_names=("zed",),
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
    return run_sync(
        machine.home,
        machine.store,
        codec=machine.codec,
        adapters={"jsonc": JsoncAdapter()},
        catalog=BuiltinCatalog.default(),
        apps=NO_APP_INFO if apps is None else apps,
        decide=decide,
        resolving=resolving,
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
    """Two machines in sync on `settings`, both tracking the Zed files."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, settings)
    _write(a, KEYMAP, _fixture("keymap.json"))
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    # Zed's directory exists, so the apply is not deferred — B simply has no file yet.
    (b.home / ".config" / "zed").mkdir(parents=True)
    _run(b)
    return a, b


# --------------------------------------------------------------------------- credentials at every layer

CREDENTIAL_MARKERS = [
    pytest.param(b"FAKE-MCP-ENV-MARKER", id="root-context-server-env"),
    pytest.param(b"FAKE-MCP-HEADER-MARKER", id="root-context-server-headers"),
    pytest.param(b"FAKE-MCP-OAUTH-MARKER", id="root-context-server-oauth"),
    pytest.param(b"FAKE-MCP-EXTENSION-MARKER", id="root-context-server-settings"),
    pytest.param(b"FAKE-TERMINAL-ENV-MARKER", id="root-terminal-env"),
    pytest.param(b"FAKE-LSP-ENV-MARKER", id="root-lsp-binary-env"),
    pytest.param(b"FAKE-AGENT-ENV-MARKER", id="root-agent-server-env"),
    pytest.param(b"FAKE-MODEL-HEADER-MARKER", id="root-model-custom-headers"),
    pytest.param(b"FAKE-DEVCONTAINER-MARKER", id="root-dev-container"),
    pytest.param(b"fakeopaquetoken0123456789abcdef", id="root-openai-compatible-headers"),
    pytest.param(b"fakeopaquetoken9876543210fedcba", id="root-anthropic-compatible-headers"),
    pytest.param(b"FAKE-MACOS-ENV-MARKER", id="macos"),
    pytest.param(b"FAKE-PREVIEW-ENV-MARKER", id="preview"),
    pytest.param(b"FAKE-PROFILE-ENV-MARKER", id="profile-settings"),
]


@pytest.mark.parametrize("marker", CREDENTIAL_MARKERS)
def test_a_credential_never_reaches_a_repo_object_and_stays_in_the_live_file(
    tmp_path, memory_key_store, marker: bytes
) -> None:
    # given — the fixture carries a credential at the root, in `macos`, in `preview` and in a profile
    a, _ = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))

    # when
    plaintexts = _decrypted_objects(a)

    # then — cut before sealing, and the file on this machine keeps it
    assert [blob for blob in plaintexts if marker in blob] == []
    assert marker in _read(a, SETTINGS)


def test_no_repo_object_holds_an_env_or_headers_key(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))

    # when
    plaintexts = _decrypted_objects(a)

    # then — the shared settings are all that was sealed
    assert [blob for blob in plaintexts if b'"env"' in blob or b'"headers"' in blob] == []
    assert any(b'"buffer_font_size": 15' in blob for blob in plaintexts)


def test_this_machines_server_env_survives_an_apply(tmp_path, memory_key_store) -> None:
    # given — A published the fixture; B's file has its own env for the same server
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, _fixture("settings.jsonc"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    own = _fixture("settings.jsonc").replace(b"FAKE-MCP-ENV-MARKER", b"FAKE-B-ENV-MARKER")
    _write(b, SETTINGS, own)

    # when
    _run(b, adopt="remote")

    # then — A's shared settings landed and B's credential is untouched
    merged = _read(b, SETTINGS)
    assert b"FAKE-B-ENV-MARKER" in merged
    assert b"FAKE-MCP-ENV-MARKER" not in merged


# --------------------------------------------------------------------------- machine-local keys


def test_a_machine_local_change_produces_no_commit(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))
    before = _git(a.repo, "rev-list", "--count", "HEAD").strip()

    # when — only a noise key and a credential change
    changed = _read(a, SETTINGS).replace(b"/usr/local/bin/node", b"/opt/homebrew/bin/node")
    _write(a, SETTINGS, changed.replace(b"FAKE-MCP-ENV-MARKER", b"FAKE-ROTATED-MARKER"))
    report = _run(a)

    # then
    assert report.unchanged is True
    assert _git(a.repo, "rev-list", "--count", "HEAD").strip() == before


def test_another_machines_node_path_is_never_overwritten(tmp_path, memory_key_store) -> None:
    # given — A published; B has its own node path
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, _fixture("settings.jsonc"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    _write(b, SETTINGS, _fixture("settings.jsonc").replace(b"/usr/local/bin/node", b"/opt/homebrew/bin/node"))
    _run(b, adopt="remote")

    # when — A changes a shared setting and B syncs
    _write(a, SETTINGS, _read(a, SETTINGS).replace(b'"buffer_font_size": 15', b'"buffer_font_size": 18'))
    _run(a)
    report = _run(b)

    # then
    assert report.has_conflicts is False
    merged = _read(b, SETTINGS)
    assert b'"buffer_font_size": 18' in merged
    assert b"/opt/homebrew/bin/node" in merged
    assert b"/usr/local/bin/node" not in merged


# --------------------------------------------------------------------------- keymap


def test_a_keymap_changed_on_both_sides_is_one_conflict_on_the_whole_array(tmp_path, memory_key_store) -> None:
    # given — each machine rebinds a different key
    a, b = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))
    original = _fixture("keymap.json")
    _write(a, KEYMAP, original.replace(b"pane::GoBack", b"pane::GoForward"))
    _run(a)
    edited_b = original.replace(b"workspace::ToggleLeftDock", b"workspace::ToggleRightDock")
    _write(b, KEYMAP, edited_b)

    # when
    report = _run(b)

    # then — the array is one value, so there is no per-binding merge; B's file is untouched
    assert report.has_conflicts is True
    assert [conflict.path for conflict in report.conflicts] == [KEYMAP]
    assert _read(b, KEYMAP) == edited_b


# --------------------------------------------------------------------------- snippets and deferral


def test_a_snippet_from_another_machine_is_applied_under_the_glob(tmp_path, memory_key_store) -> None:
    # given — A has a language snippet; B has Zed's directory but no snippet yet
    a, _ = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))
    snippet = SNIPPETS.replace("*", "javascript")
    _write(a, snippet, b'{\n  "log": {\n    "prefix": "log",\n    "body": "console.log($1)"\n  }\n}\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-snippet", a.remote)
    _initialize(b, import_from=a.recovery)
    (b.home / ".config" / "zed" / "snippets").mkdir(parents=True)

    # when
    report = _run(b)

    # then
    assert snippet in report.applied
    assert _read(b, snippet) == _read(a, snippet)
    assert report.deferred == ()


def test_a_machine_without_zed_defers_with_the_download_page(tmp_path, memory_key_store) -> None:
    # given — A published; B has no Zed, and the detector says it is not installed
    a, _ = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))
    b = _machine(tmp_path, memory_key_store, "machine-uninstalled", a.remote)
    _initialize(b, import_from=a.recovery)
    apps = _StubApps(False)

    # when
    report = _run(b, apps=apps)

    # then
    assert sorted(deferral.path for deferral in report.deferred) == sorted((SETTINGS, KEYMAP))
    assert {deferral.download_url for deferral in report.deferred} == {"https://zed.dev/download"}
    assert {deferral.cask for deferral in report.deferred} == {"zed"}
    # syncintosh's own config lives under `.config`, so the check is on Zed's directory
    assert (b.home / ".config" / "zed").exists() is False
    assert apps.queried == ["dev.zed.Zed"]


def test_a_theme_symlinked_to_the_dev_builds_sign_in_file_does_not_publish_it(tmp_path, memory_key_store) -> None:
    # given — the `themes/*.json` glob is the one route that could reach the plaintext sign-in file
    a = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(a)
    _write(a, SETTINGS, _fixture("settings.jsonc"))
    _write(a, ".config/zed/development_credentials", b'{"token": "FAKE-DEV-SIGN-IN-MARKER"}')
    (a.home / ".config" / "zed" / "themes").mkdir()
    (a.home / ".config" / "zed" / "themes" / "evil.json").symlink_to(
        a.home / ".config" / "zed" / "development_credentials"
    )

    # when
    _run(a)

    # then — a symlink is reported rather than followed, and the file itself is no preset path
    assert [blob for blob in _decrypted_objects(a) if b"FAKE-DEV-SIGN-IN-MARKER" in blob] == []


# --------------------------------------------------------------------------- an item deleted on the other Mac

GITHUB_SERVER = (
    b'    "github": {\n'
    b'      "command": "npx",\n'
    b'      "args": ["-y", "@modelcontextprotocol/server-github"],\n'
    b'      "env": {\n'
    b'        "GITHUB_PERSONAL_ACCESS_TOKEN": "FAKE-MCP-ENV-MARKER",\n'
    b"      },\n"
    b"    },\n"
)


def _pair_with_local_values(tmp_path, memory_key_store) -> tuple[Machine, Machine]:
    """Two Macs in sync where B's own file carries every local value the fixture has."""
    a, b = _pair(tmp_path, memory_key_store, _fixture("settings.jsonc"))
    _write(b, SETTINGS, _fixture("settings.jsonc"))
    _run(b)
    return a, b


def test_a_server_deleted_on_the_other_mac_leaves_no_half_server_here(tmp_path, memory_key_store) -> None:
    # given — both Macs in sync; B's own file holds the local values of every server
    a, b = _pair_with_local_values(tmp_path, memory_key_store)
    assert GITHUB_SERVER in _read(a, SETTINGS)
    assert b"FAKE-MCP-ENV-MARKER" in _read(b, SETTINGS)

    # when — A deletes the `github` server and syncs, then B syncs
    _write(a, SETTINGS, _read(a, SETTINGS).replace(GITHUB_SERVER, b""))
    _run(a)
    report = _run(b)

    # then — the server is gone from B's file, env included, and the server that stayed keeps its local values
    merged = _read(b, SETTINGS)
    assert report.has_conflicts is False
    assert [(lost.path, lost.item) for lost in report.dropped] == [(SETTINGS, "context_servers.github")]
    assert report.dropped[0].keys == 1
    assert b"FAKE-MCP-ENV-MARKER" not in repr(report).encode()
    assert b'"github"' not in merged
    assert b"FAKE-MCP-ENV-MARKER" not in merged
    assert b"FAKE-MCP-HEADER-MARKER" in merged
    assert b"FAKE-MCP-OAUTH-MARKER" in merged


def test_a_profile_deleted_on_the_other_mac_is_gone_here_too(tmp_path, memory_key_store) -> None:
    # given — `profiles` is the last member of the fixture
    a, b = _pair_with_local_values(tmp_path, memory_key_store)
    original = _read(a, SETTINGS)
    start = original.index(b'  "profiles": {')
    assert b"FAKE-PROFILE-ENV-MARKER" in _read(b, SETTINGS)

    # when — A deletes the profile and syncs, then B syncs
    _write(a, SETTINGS, original[:start] + b"}\n")
    _run(a)
    report = _run(b)

    # then — B's profile, with the credential and the font size it held, is removed whole
    merged = _read(b, SETTINGS)
    assert b'"profiles"' not in merged
    assert b"FAKE-PROFILE-ENV-MARKER" not in merged
    assert b'"work"' not in merged
    assert [(lost.path, lost.item) for lost in report.dropped] == [(SETTINGS, "profiles.work")]
    assert report.dropped[0].keys == 1
    assert b"FAKE-PROFILE-ENV-MARKER" not in repr(report).encode()


def test_a_deleted_profile_takes_an_env_only_server_inside_it(tmp_path, memory_key_store) -> None:
    # given — B's profile also holds a server of its own with nothing but a local credential map
    a, b = _pair_with_local_values(tmp_path, memory_key_store)
    anchor = b'      "settings": {\n        "terminal": {'
    server = b'"context_servers": { "local-only": { "env": { "K": "FAKE-PROFILE-SERVER-MARKER" } } },'
    local = b'      "settings": {\n        ' + server + b'\n        "terminal": {'
    _write(b, SETTINGS, _read(b, SETTINGS).replace(anchor, local, 1))
    assert b"FAKE-PROFILE-SERVER-MARKER" in _read(b, SETTINGS)
    original = _read(a, SETTINGS)

    # when — A deletes the profile and syncs, then B syncs
    _write(a, SETTINGS, original[: original.index(b'  "profiles": {')] + b"}\n")
    _run(a)
    report = _run(b)

    # then — neither the profile nor the server's credential map is left behind
    merged = _read(b, SETTINGS)
    assert b'"work"' not in merged
    assert b"FAKE-PROFILE-SERVER-MARKER" not in merged
    assert [(lost.item, lost.keys) for lost in report.dropped] == [("profiles.work", 2)]
    assert b"FAKE-PROFILE-SERVER-MARKER" not in repr(report).encode()


def test_an_adopt_remote_overwrite_reports_the_local_server_it_removed(tmp_path, memory_key_store) -> None:
    # given — the repo holds the fixture; B's own file is a different server with a local credential map
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, _fixture("settings.jsonc"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    own = (
        b'{\n  "context_servers": {\n    "mine": { "command": "x", "env": { "K": "FAKE-ADOPT-ENV-MARKER" } }\n  }\n}\n'
    )
    _write(b, SETTINGS, own)

    # when
    report = _run(b, adopt="remote")

    # then — the overwrite removed the server and its credential map, and says so
    assert b"FAKE-ADOPT-ENV-MARKER" not in _read(b, SETTINGS)
    assert [(lost.path, lost.item, lost.keys) for lost in report.dropped] == [(SETTINGS, "context_servers.mine", 1)]
    assert b"FAKE-ADOPT-ENV-MARKER" not in repr(report).encode()


def test_a_server_that_exists_only_here_with_only_settings_survives_a_sync(tmp_path, memory_key_store) -> None:
    # given — B adds an extension server that has only `settings`; it is stripped whole, so it is in no object
    a, b = _pair_with_local_values(tmp_path, memory_key_store)
    assert b"FAKE-MCP-ENV-MARKER" in _read(b, SETTINGS)
    assert b"FAKE-PROFILE-ENV-MARKER" in _read(b, SETTINGS)
    local = b'    "mine-ext": {\n      "settings": {\n        "token": "FAKE-LOCAL-EXT-MARKER",\n      },\n    },\n'
    _write(b, SETTINGS, _read(b, SETTINGS).replace(b'    "from-extension": {', local + b'    "from-extension": {'))

    # when — A changes a shared setting and B syncs
    _write(a, SETTINGS, _read(a, SETTINGS).replace(b'"buffer_font_size": 15', b'"buffer_font_size": 18'))
    _run(a)
    report = _run(b)

    # then — the shared change landed and the local-only server is untouched, with nothing reported as lost
    merged = _read(b, SETTINGS)
    assert report.dropped == ()
    assert b'"buffer_font_size": 18' in merged
    assert b"FAKE-LOCAL-EXT-MARKER" in merged
    assert [blob for blob in _decrypted_objects(b) if b"FAKE-LOCAL-EXT-MARKER" in blob] == []
