# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a project's agent memory, recorded by project name, across two Macs that keep it apart (#56).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The preset is the shipped
`claude-memory`. Machine A keeps the project at `src/x` and B at `code/x`, so Claude Code's folder for it
(`claude_project_dir_name` of the real root) differs between them; the file contents are invented.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.agent_projects import AgentLayout, AgentProject, claude_project_dir_name
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.claude_memory import CLAUDE_PROJECTS
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncPorts, SyncReport, sync
from syncintosh.system.agent_project_index import SystemAgentProjectIndex
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
CATALOG = BuiltinCatalog.default()
INDEX_TEXT = b"- [Role](user-role.md) - who the user is\n"
FACT_TEXT = b"---\nname: user-role\ntype: user\n---\n\nA data scientist. Invented for this test.\n"
MEMORY = ".claude/projects/@work/memory/MEMORY.md"
FACT = ".claude/projects/@work/memory/user-role.md"


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
    root: str
    recovery: Path | None = None
    environ: dict[str, str] = field(default_factory=dict)
    codec: KeyMaterial = field(default_factory=lambda: KeyMaterial(params=CHEAP, allowed=ALLOWED))


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


def _machine(tmp_path: Path, make_store, name: str, remote: str, root: str) -> Machine:
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
        root=root,
    )


def _map(machine: Machine, project: str | None) -> None:
    """Write this machine's config, mapping its root under `project` (or no project at all)."""
    base = new_machine_config(remote=machine.remote, machine_id=machine.machine_id, preset_names=(), catalog=CATALOG)
    projects = (
        ()
        if project is None
        else (
            AgentProject.create(
                name=project,
                path=machine.root,
                layouts=(AgentLayout.create(preset="claude-memory", spec=CLAUDE_PROJECTS),),
            ),
        )
    )
    save_config(replace(base, agent_projects=projects), machine.config_file)


def _initialize(machine: Machine, *, import_from: Path | None = None, project: str | None = "work") -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    _map(machine, project)
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


def _run(machine: Machine) -> SyncReport:
    active = GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    return sync(
        SyncPorts.create(
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
            agent_projects=SystemAgentProjectIndex(str(machine.home), SystemDenyList(), machine.environ),
        )
    )


def _folder(machine: Machine) -> Path:
    """Where Claude Code keeps this machine's project: named after the real root, so it differs per machine."""
    return (
        machine.home / ".claude" / "projects" / claude_project_dir_name(os.path.realpath(machine.home / machine.root))
    )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _records(machine: Machine) -> list[str]:
    transport = GitCLITransport(machine.repo)
    sealed = transport.read_blob(transport.head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return sorted(record.path for record in manifest.records.values())


def _pair(tmp_path: Path, make_store):
    """A keeps the project at `src/x` and has synced its memory; B is initialised with the project at `code/x`."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote, "src/x")
    (a.home / a.root).mkdir(parents=True)
    _initialize(a)
    _write(_folder(a) / "memory" / "MEMORY.md", INDEX_TEXT)
    _write(_folder(a) / "memory" / "user-role.md", FACT_TEXT)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote, "code/x")
    (b.home / b.root).mkdir(parents=True)
    _initialize(b, import_from=a.recovery)
    return a, b


def test_the_two_machines_keep_the_project_under_different_folders(tmp_path, memory_key_store) -> None:
    # when
    a, b = _pair(tmp_path, memory_key_store)

    # then — the premise of the whole feature
    assert _folder(a).name != _folder(b).name


def test_memory_collected_on_one_mac_lands_in_the_folder_the_other_mac_derives(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)

    # when
    report = _run(b)

    # then
    assert report.has_conflicts is False
    assert (_folder(b) / "memory" / "MEMORY.md").read_bytes() == INDEX_TEXT
    assert (_folder(b) / "memory" / "user-role.md").read_bytes() == FACT_TEXT
    assert _folder(a).exists() and sorted(p.name for p in (b.home / ".claude" / "projects").iterdir()) == [
        _folder(b).name
    ]


def test_records_carry_the_project_name_and_no_folder_or_filename_names_a_root(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)

    # when
    names = sorted(p.name for p in (a.repo / "objects").iterdir())

    # then
    assert _records(a) == [MEMORY, FACT]
    assert [n for n in names if "work" in n or "src" in n or "Users" in n or "memory" in n] == []


def test_an_edit_on_either_mac_reaches_the_other(tmp_path, memory_key_store) -> None:
    # given — both in sync
    a, b = _pair(tmp_path, memory_key_store)
    _run(b)

    # when — B edits one fact and adds another, A syncs; then A edits the index and B syncs
    _write(_folder(b) / "memory" / "user-role.md", FACT_TEXT + b"More.\n")
    _write(_folder(b) / "memory" / "new-fact.md", b"A new fact.\n")
    _run(b)
    _run(a)
    _write(_folder(a) / "memory" / "MEMORY.md", INDEX_TEXT + b"- [New](new-fact.md)\n")
    _run(a)
    _run(b)

    # then
    assert (_folder(a) / "memory" / "user-role.md").read_bytes().endswith(b"More.\n")
    assert (_folder(a) / "memory" / "new-fact.md").read_bytes() == b"A new fact.\n"
    assert (_folder(b) / "memory" / "MEMORY.md").read_bytes().endswith(b"(new-fact.md)\n")


def test_a_name_this_machine_does_not_map_is_never_written(tmp_path, memory_key_store) -> None:
    # given — B maps a different name, so the record's `@work` is not one it allows
    _, b = _pair(tmp_path, memory_key_store)
    _map(b, "other")

    # when
    report = _run(b)

    # then
    assert [r for r in report.refused if r.path == MEMORY and r.blocking is False] != []
    assert _folder(b).exists() is False


def test_nothing_is_collected_without_a_mapping(tmp_path, memory_key_store) -> None:
    # given — memory exists on disk, but this machine maps no project
    remote = _bare_remote(tmp_path)
    c = _machine(tmp_path, memory_key_store, "machine-c", remote, "src/x")
    (c.home / c.root).mkdir(parents=True)
    _initialize(c, project=None)
    _write(_folder(c) / "memory" / "MEMORY.md", INDEX_TEXT)

    # when
    report = _run(c)

    # then
    assert (report.collected, _records(c)) == ((), [])


def test_a_project_that_is_not_cloned_here_defers_and_creates_nothing(tmp_path, memory_key_store) -> None:
    # given
    _, b = _pair(tmp_path, memory_key_store)
    (b.home / b.root).rmdir()

    # when
    report = _run(b)

    # then
    assert [d for d in report.deferred if d.path == MEMORY and "not on this machine" in d.reason] != []
    assert (b.home / ".claude").exists() is False


def test_an_environment_override_collects_and_writes_nothing(tmp_path, memory_key_store) -> None:
    # given — the agent's data was moved by an environment variable, so the derived folder cannot be trusted
    _, b = _pair(tmp_path, memory_key_store)
    b.environ["CLAUDE_CONFIG_DIR"] = str(b.home / "elsewhere")

    # when
    report = _run(b)

    # then
    assert [d for d in report.deferred if d.path == MEMORY and "CLAUDE_CONFIG_DIR" in d.reason] != []
    assert (b.home / ".claude").exists() is False


def test_a_relocated_memory_directory_is_skipped_and_the_records_are_kept(tmp_path, memory_key_store) -> None:
    # given — B's project settings move the memory directory elsewhere
    a, b = _pair(tmp_path, memory_key_store)
    _write(b.home / b.root / ".claude" / "settings.json", b'{"autoMemoryDirectory": "invented"}')
    before = _records(a)

    # when
    report = _run(b)

    # then
    assert [d for d in report.deferred if d.path == MEMORY and "autoMemoryDirectory" in d.reason] != []
    assert _folder(b).exists() is False
    assert _records(b) == before


def test_a_raw_project_folder_named_in_the_config_is_refused_not_collected(tmp_path, memory_key_store) -> None:
    # given — a hand-edited config tracks the real folder directly, which no record path may be
    a, _ = _pair(tmp_path, memory_key_store)
    raw = f".claude/projects/{_folder(a).name}/memory/MEMORY.md"
    config = load_config(a.config_file)
    save_config(
        replace(config, items=(LocalItem.create(path=raw, preset="custom", scope="common", format="text"),)),
        a.config_file,
    )

    # when
    report = _run(a)

    # then
    assert [r for r in report.refused if r.path == raw] != []
    assert raw not in _records(a)
