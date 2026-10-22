# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the Docker preset strips `auths` and every credential-shaped key in code.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
The catalog is the one the CLI wires, so the policy under test is the shipped one. The fixture
carries a real credential map and real context TLS material, because a test that cannot leak
proves nothing (``AGENTS.md`` §3.4).
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
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.docker import CONFIG, DAEMON
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.sync import SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.json_check import JsonSyntaxValidator
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
TLS_KEY = ".docker/contexts/tls/key.pem"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "docker"


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


def _docker_config(machine: Machine, *, preset: str = "docker", extra: tuple[LocalItem, ...] = ()) -> SyncConfig:
    return SyncConfig.create(
        remote=machine.remote,
        machine_id=machine.machine_id,
        items=(
            LocalItem.create(path=CONFIG, preset=preset, scope="common", format="jsonc"),
            LocalItem.create(path=DAEMON, preset=preset, scope="common", format="jsonc"),
            *extra,
        ),
    )


def _initialize(machine: Machine, config: SyncConfig, *, import_from: Path | None = None) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
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
        # The preset names the strict JSON check, and a missing one is refused rather than skipped,
        # so the harness wires what the CLI wires.
        validators={"json": JsonSyntaxValidator()},
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


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", BRANCH, str(remote)], capture_output=True, check=True)
    return str(remote)


# --------------------------------------------------------------------------- the cut


def test_a_collected_docker_config_never_carries_auths(tmp_path, memory_key_store) -> None:
    # given — a machine whose `config.json` is the credential-bearing fixture
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _docker_config(machine))
    _write(machine, CONFIG, _fixture("config.json"))
    _write(machine, DAEMON, _fixture("daemon.json"))

    # when
    _run(machine)
    plaintexts = _decrypted_objects(machine)

    # then — neither the credential map nor the token is in any object at all
    assert plaintexts != []
    assert [blob for blob in plaintexts if b"auths" in blob] == []
    assert [blob for blob in plaintexts if b"dXNlcjpwYXNzd29yZA==" in blob] == []
    assert [blob for blob in plaintexts if b"ZGV2OnNlY3JldA==" in blob] == []
    # the parts that are settings do travel
    assert any(b"defaultKeepStorage" in blob for blob in plaintexts)
    # a helper binary's name is this machine's own, so it does not travel either
    assert [blob for blob in plaintexts if b"credsStore" in blob] == []
    # and this machine's own file keeps its credentials and its helper name
    assert b"auths" in _read(machine, CONFIG)
    assert b'"credsStore": "osxkeychain"' in _read(machine, CONFIG) or b'"credsStore": "desktop"' in _read(
        machine, CONFIG
    )


def test_documented_credential_carriers_never_reach_an_object(tmp_path, memory_key_store) -> None:
    # given — a config.json with an `Authorization` header, and a daemon.json with a proxy password
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _docker_config(machine))
    _write(machine, CONFIG, _fixture("config.json"))
    _write(machine, DAEMON, _fixture("daemon.json"))

    # when
    _run(machine)
    plaintexts = _decrypted_objects(machine)

    # then — the keys were cut before sealing, in both files, and the local files keep them
    markers = (b"HEADER-TOKEN-MARKER", b"PROXY-PW-MARKER")
    leaked = [marker for marker in markers for blob in plaintexts if marker in blob]
    assert leaked == []
    assert [blob for blob in plaintexts if b"HttpHeaders" in blob] == []
    assert [blob for blob in plaintexts if b"proxies" in blob] == []
    assert b"HEADER-TOKEN-MARKER" in _read(machine, CONFIG)
    assert b"PROXY-PW-MARKER" in _read(machine, DAEMON)


def test_a_user_preset_name_still_strips_auths(tmp_path, memory_key_store) -> None:
    # given — the path is tracked under a name the built-in catalog does not know
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _docker_config(machine, preset="my-own-thing"))
    _write(machine, CONFIG, _fixture("config.json"))

    # when — the policy is looked up by path, never by the name a record carries
    _run(machine)

    # then
    plaintexts = _decrypted_objects(machine)
    assert [blob for blob in plaintexts if b"auths" in blob] == []


def test_apply_keeps_this_machines_docker_auths(tmp_path, memory_key_store) -> None:
    # given — A published a config.json; B's own file holds a different credential
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _docker_config(a))
    _write(a, CONFIG, _fixture("config.json"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _docker_config(b), import_from=a.recovery)
    local = (
        _fixture("config.json")
        .replace(b"dXNlcjpwYXNzd29yZA==", b"bG9jYWw6b25seQ==")
        .replace(b'"credsStore": "desktop"', b'"credsStore": "osxkeychain"')
    )
    _write(b, CONFIG, local)

    # when — B takes the repo's side
    _run(b, adopt="remote")

    # then — the repo's settings landed, and B's credential *and* its helper name survived: A runs
    # Docker Desktop and B runs colima, so A's `desktop` helper binary does not exist on B
    merged = _read(b, CONFIG)
    assert b"bG9jYWw6b25seQ==" in merged
    assert b'"credsStore": "osxkeychain"' in merged
    assert b'"desktop"' not in merged


def test_a_machine_without_a_docker_config_gets_one_without_auths(tmp_path, memory_key_store) -> None:
    # given — A published the non-secret parts; B has Docker installed but has never run it
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _docker_config(a))
    _write(a, CONFIG, _fixture("config.json"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _docker_config(b), import_from=a.recovery)
    (b.home / ".docker").mkdir(parents=True)

    # when
    report = _run(b)

    # then — the file is created with the shared settings, and neither a credential nor another
    # machine's helper name is invented for it
    assert CONFIG in report.applied
    created = _read(b, CONFIG)
    assert b"auths" not in created
    assert b"credsStore" not in created
    assert b"currentContext" not in created


def test_a_machine_without_the_docker_directory_defers(tmp_path, memory_key_store) -> None:
    # given — A published a config.json and B has no Docker at all
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _docker_config(a))
    _write(a, CONFIG, _fixture("config.json"))
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _docker_config(b), import_from=a.recovery)

    # when
    report = _run(b)

    # then — reported instead of creating a directory tree (the app is not installed)
    assert sorted(deferral.path for deferral in report.deferred) == sorted((CONFIG, DAEMON))
    assert (b.home / ".docker").exists() is False


def test_a_current_context_only_change_produces_no_commit(tmp_path, memory_key_store) -> None:
    # given — a settled machine, and the endpoint it talks to is machine-local
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _docker_config(machine))
    _write(machine, CONFIG, _fixture("config.json"))
    _run(machine)
    before = _git(machine.repo, "rev-list", "--count", "HEAD").strip()

    # when — the user switches context
    _write(machine, CONFIG, _fixture("config.json").replace(b'"desktop-linux"', b'"remote-builder"'))
    report = _run(machine)

    # then — no diff, no commit, and the local choice is left alone
    assert report.unchanged is True
    assert _git(machine.repo, "rev-list", "--count", "HEAD").strip() == before
    assert b'"remote-builder"' in _read(machine, CONFIG)


def test_a_corrupt_docker_config_is_refused_and_nothing_is_written(tmp_path, memory_key_store) -> None:
    # given — a config.json that is not valid JSONC
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _docker_config(machine))
    _write(machine, CONFIG, b'{\n  "auths": {\n')

    # when
    report = _run(machine)

    # then — fail-closed: reported, never sealed raw, and no object written
    assert [refusal.path for refusal in report.unprojectable] == [CONFIG]
    assert FileRepo(machine.repo).list_paths("objects") == ()
    assert _read(machine, CONFIG) == b'{\n  "auths": {\n'


# --------------------------------------------------------------------------- deny-list


def test_docker_context_tls_material_is_never_collected(tmp_path, memory_key_store) -> None:
    # given — an item pointing straight at a context private key
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    item = LocalItem.create(path=TLS_KEY, preset="docker", scope="common", format="text")
    _initialize(machine, _docker_config(machine, extra=(item,)))
    _write(machine, TLS_KEY, _fixture("contexts/tls/key.pem"))

    # when
    report = _run(machine)

    # then — refused before it is read, and reported (AGENTS.md §3.4)
    assert TLS_KEY in report.skipped
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_case_variant_of_the_tls_directory_is_also_refused(tmp_path, memory_key_store) -> None:
    # given — APFS is case-insensitive, so this names the very same file
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    variant = ".DOCKER/Contexts/TLS/key.pem"
    item = LocalItem.create(path=variant, preset="docker", scope="common", format="text")
    _initialize(machine, _docker_config(machine, extra=(item,)))
    _write(machine, TLS_KEY, _fixture("contexts/tls/key.pem"))

    # when
    report = _run(machine)

    # then
    assert variant in report.skipped
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_config_that_is_not_strict_json_is_never_written(tmp_path, memory_key_store) -> None:
    # given — A's `config.json` holds a trailing comma in a key the projection keeps: JSONC parses
    # it, Docker's strict reader does not, so A publishes bytes a strict reader would refuse
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _docker_config(a))
    _write(a, CONFIG, b'{\n  "experimental": "enabled",\n}\n')
    first = _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _docker_config(b), import_from=a.recovery)
    (b.home / ".docker").mkdir(parents=True)

    # when
    report = _run(b)

    # then — the payload really is what a strict reader refuses, and the write is refused with a
    # reason instead of handing B a file Docker cannot read. A's own file is untouched: it is the
    # user's, and A is the machine whose Docker is already in that state.
    with pytest.raises(ValueError):
        json.loads(_read(a, CONFIG))
    assert first.invalid == ()
    assert _read(a, CONFIG) == b'{\n  "experimental": "enabled",\n}\n'
    assert [refusal.path for refusal in report.invalid] == [CONFIG]
    assert (b.home / CONFIG).exists() is False
