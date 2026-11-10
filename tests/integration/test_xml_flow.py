# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: an XML file through collect → encrypt → commit → pull → decrypt → merge → apply.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1). The catalog is a test
preset over one XML file, since no shipped preset is XML yet; the adapter is the one the CLI wires.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _harness import run_sync

from syncintosh.adapters.xml import XmlAdapter
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import SyncConfig
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.policy import policy_digest
from syncintosh.core.ports import AppDetector
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.core.resolution import Decide
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
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
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "xml"
OPTIONS = ".config/xmltest/options.xml"

# A test preset: the shipped catalog has no XML preset until the JetBrains ones land, and this flow only needs one.
XML_PRESET = Preset.create(name="xmltest", files=(PresetFile.create(path=OPTIONS, format="xml"),))


@dataclass(frozen=True)
class _TestCatalog(BuiltinCatalog):
    """The test preset with a history of its own digest: it is not in the shipped `POLICY_HISTORY`."""

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        return (policy_digest(XML_PRESET),) if name == XML_PRESET.name else None


CATALOG = _TestCatalog.create((XML_PRESET,))


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
    return new_machine_config(
        remote=machine.remote,
        machine_id=machine.machine_id,
        preset_names=("xmltest",),
        catalog=CATALOG,
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
    GitCLITransport(machine.repo)
    EnvelopeCipher(machine.store)
    return run_sync(
        machine.home,
        machine.store,
        codec=machine.codec,
        adapters={"xml": XmlAdapter()},
        catalog=CATALOG,
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


def _pair(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """Two machines in sync on the fixture."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _write(a, OPTIONS, _fixture("options.xml"))
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    (b.home / ".config" / "xmltest").mkdir(parents=True)
    _run(b)
    return a, b


def test_an_xml_file_reaches_the_other_machine_byte_for_byte(tmp_path, memory_key_store) -> None:
    # when
    a, b = _pair(tmp_path, memory_key_store)

    # then — the IDE's own layout survives the trip
    assert _read(b, OPTIONS) == _fixture("options.xml")
    assert _read(a, OPTIONS) == _fixture("options.xml")


def test_changes_to_different_keys_on_two_machines_converge(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, OPTIONS, _read(a, OPTIONS).replace(b'value="14"', b'value="16"'))
    _write(b, OPTIONS, _read(b, OPTIONS).replace(b"Example Mono", b"Other Mono"))
    _run(a)

    # when
    report_b = _run(b)
    _run(a)

    # then — a key merge, not a line merge: both edits are in both files, and nothing else moved
    expected = _fixture("options.xml").replace(b'value="14"', b'value="16"').replace(b"Example Mono", b"Other Mono")
    assert report_b.has_conflicts is False
    assert _read(a, OPTIONS) == expected
    assert _read(b, OPTIONS) == expected


def test_an_added_array_element_and_an_added_component_merge(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(
        a,
        OPTIONS,
        _read(a, OPTIONS).replace(b"    <mask>*.xml</mask>\n", b"    <mask>*.xml</mask>\n    <mask>*.py</mask>\n"),
    )
    _write(
        b,
        OPTIONS,
        _read(b, OPTIONS).replace(
            b'  <component name="Empty" />\n', b'  <component name="Added" />\n  <component name="Empty" />\n'
        ),
    )
    _run(a)

    # when
    report_b = _run(b)

    # then
    merged = _read(b, OPTIONS)
    assert report_b.has_conflicts is False
    assert b"<mask>*.py</mask>" in merged
    assert b'<component name="Added" />' in merged


def test_the_same_key_changed_differently_is_a_conflict_and_the_live_file_is_untouched(
    tmp_path, memory_key_store
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(a, OPTIONS, _read(a, OPTIONS).replace(b'value="14"', b'value="16"'))
    mine = _read(b, OPTIONS).replace(b'value="14"', b'value="18"')
    _write(b, OPTIONS, mine)
    _run(a)

    # when
    report_b = _run(b)

    # then
    assert report_b.has_conflicts is True
    assert _read(b, OPTIONS) == mine


def test_a_file_that_is_not_xml_is_reported_and_not_published(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    broken = _read(a, OPTIONS).replace(b"</application>", b"")
    _write(a, OPTIONS, broken)
    _write(b, OPTIONS, _read(b, OPTIONS).replace(b'value="14"', b'value="18"'))
    _run(b)

    # when
    report_a = _run(a)

    # then — a malformed side is a conflict, never a half-written merge
    assert report_a.has_conflicts is True
    assert _read(a, OPTIONS) == broken
