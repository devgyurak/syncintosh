# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the iTerm2 preset — Dynamic Profiles merged by `Guid`, and deferral on a machine without iTerm2.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1). The fixture carries
real Dynamic Profile key names with invented values. The catalog is the one the CLI wires.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _harness import run_sync

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import SyncConfig
from syncintosh.core.ports import AppDetector
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.core.resolution import Decide
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.iterm2 import DYNAMIC_PROFILES
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.resolve import decision, take_chooser
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
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "iterm2" / "dynamic-profiles.json"
PROFILES_FILE = DYNAMIC_PROFILES.replace("*", "work")
PROFILES_DIR = Path(PROFILES_FILE).parent
WORK_GUID = "11111111-AAAA-4AAA-8AAA-000000000001"
LOGS_GUID = "11111111-AAAA-4AAA-8AAA-000000000002"


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
    return new_machine_config(
        remote=machine.remote,
        machine_id=machine.machine_id,
        preset_names=("iterm2",),
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


class _NotRunning:
    """iTerm2 is closed: the preset waits for that before it writes (`defer_while_running`)."""

    def running(self, bundle_id: str) -> bool | None:
        return False


def _run(
    machine: Machine,
    *,
    apps: AppDetector | None = None,
    decide: Decide | None = None,
    resolving: bool = False,
) -> SyncReport:
    return run_sync(
        machine.home,
        machine.store,
        codec=machine.codec,
        adapters={"jsonc": JsoncAdapter()},
        validators={},
        catalog=BuiltinCatalog.default(),
        apps=NO_APP_INFO if apps is None else apps,
        running=_NotRunning(),
        decide=decide,
        resolving=resolving,
    )


def _write(machine: Machine, relpath: str, data: bytes) -> None:
    target = machine.home / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _read(machine: Machine, relpath: str) -> bytes:
    return (machine.home / relpath).read_bytes()


def _profiles(machine: Machine) -> dict[str, dict[str, Any]]:
    """The profiles in this machine's live file, by `Guid`."""
    return {each["Guid"]: each for each in json.loads(_read(machine, PROFILES_FILE))["Profiles"]}


def _edited(base: bytes, **changes: str) -> bytes:
    """The fixture with `Badge Text` of the profiles named by Guid replaced (`work`/`logs` are the fixture's badges)."""
    return base.replace(b'"Badge Text": "work"', f'"Badge Text": "{changes.get("work", "work")}"'.encode()).replace(
        b'"Badge Text": "logs"', f'"Badge Text": "{changes.get("logs", "logs")}"'.encode()
    )


def _added(base: bytes, guid: str, name: str) -> bytes:
    """The fixture with one more profile appended to `Profiles` (the fixture ends `}\\n  ]\\n}`)."""
    return base.replace(b"\n  ]", f',\n    {{"Name": "{name}", "Guid": "{guid}"}}\n  ]'.encode())


def _pair(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """Two machines in sync on the fixture; iTerm2's DynamicProfiles directory exists on both."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _write(a, PROFILES_FILE, FIXTURE.read_bytes())
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    (b.home / PROFILES_DIR).mkdir(parents=True)
    _run(b)
    return a, b


def test_two_machines_converge_on_the_same_profiles_file(tmp_path, memory_key_store) -> None:
    # when
    a, b = _pair(tmp_path, memory_key_store)

    # then
    assert _read(b, PROFILES_FILE) == _read(a, PROFILES_FILE)


def test_a_profile_added_on_each_machine_is_kept_on_both(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    base = FIXTURE.read_bytes()
    _write(a, PROFILES_FILE, _added(base, "11111111-AAAA-4AAA-8AAA-0000000000A1", "From A"))
    _run(a)
    _write(b, PROFILES_FILE, _added(base, "11111111-AAAA-4AAA-8AAA-0000000000B1", "From B"))

    # when
    report = _run(b)

    # then — both additions, and the two profiles that were already there
    assert report.has_conflicts is False
    assert sorted(each["Name"] for each in _profiles(b).values()) == [
        "Fixture Logs",
        "Fixture Work",
        "From A",
        "From B",
    ]
    _run(a)
    assert _read(a, PROFILES_FILE) == _read(b, PROFILES_FILE)


def test_the_same_profile_changed_on_both_sides_is_a_conflict_on_that_profile_only(tmp_path, memory_key_store) -> None:
    # given — both machines change the work profile's badge; B also changes the logs profile, which A did not touch
    a, b = _pair(tmp_path, memory_key_store)
    base = FIXTURE.read_bytes()
    _write(a, PROFILES_FILE, _edited(base, work="from-a"))
    _run(a)
    edited_b = _edited(base, work="from-b", logs="from-b-logs")
    _write(b, PROFILES_FILE, edited_b)

    # when
    report = _run(b)

    # then — one conflict on the file; B's file is untouched until a pick settles it
    assert (report.has_conflicts, [each.path for each in report.conflicts], _read(b, PROFILES_FILE)) == (
        True,
        [PROFILES_FILE],
        edited_b,
    )

    # when — take A's version of the work profile
    _run(b, decide=decision(take_chooser("remote")), resolving=True)

    # then — A's badge for the conflicting profile, and B's own edit of the other kept
    assert [_profiles(b)[guid]["Badge Text"] for guid in (WORK_GUID, LOGS_GUID)] == ["from-a", "from-b-logs"]


def test_a_machine_without_iterm2_defers_with_the_download_page(tmp_path, memory_key_store) -> None:
    # given — A published; B has no iTerm2 and no live file, and the detector says it is not installed
    a, _ = _pair(tmp_path, memory_key_store)
    b = _machine(tmp_path, memory_key_store, "machine-uninstalled", a.remote)
    _initialize(b, import_from=a.recovery)
    apps = _StubApps(False)

    # when
    report = _run(b, apps=apps)

    # then
    assert [(each.path, each.download_url, each.cask) for each in report.deferred if each.path == PROFILES_FILE] == [
        (PROFILES_FILE, "https://iterm2.com/downloads.html", "iterm2")
    ]
    assert ((b.home / PROFILES_DIR).exists(), apps.queried) == (False, ["com.googlecode.iterm2"])


def test_a_machine_whose_profile_directory_is_missing_defers_when_the_app_cannot_be_detected(
    tmp_path, memory_key_store
) -> None:
    # given — the detector cannot tell, and there is no DynamicProfiles directory to write into
    a, _ = _pair(tmp_path, memory_key_store)
    b = _machine(tmp_path, memory_key_store, "machine-unknown", a.remote)
    _initialize(b, import_from=a.recovery)

    # when
    report = _run(b, apps=_StubApps(None))

    # then — nothing is created, and the row carries no download suggestion
    assert (
        [(each.path, each.download_url) for each in report.deferred if each.path == PROFILES_FILE],
        (b.home / PROFILES_DIR).exists(),
    ) == (
        [(PROFILES_FILE, None)],
        False,
    )
