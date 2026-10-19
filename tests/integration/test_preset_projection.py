# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the built-in catalog, collect-side projection, and the zsh syntax gate.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
The catalog and the validator are the ones the CLI wires, so these run the real policy rather
than a stub. The built-in catalog has no keyed-format preset yet, so the JSONC cases add one
policy beside it — the machinery under test is the same one the VS Code preset will use.
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
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.paths import collision_key
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
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
from syncintosh.system.zsh_check import ZshSyntaxValidator
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
ZSH = ".zshrc"

JSONC = "Library/Application Support/Code/User/settings.json"
TOML = "Config/app/config.toml"
SECRET = "local-proxy-password"

JSONC_POLICY = PresetFile.create(
    path=JSONC,
    format="jsonc",
    noise={("window", "zoomLevel")},
    excluded={("http", "proxyAuthorization")},
)
JSONC_PRESET = Preset.create(name="keyed-test", files=(JSONC_POLICY,))
TOML_PRESET = Preset.create(name="toml-test", files=(PresetFile.create(path=TOML, format="toml", noise={("noise",)}),))


class _Catalog:
    """The built-in catalog plus one extra preset, so projection has a keyed format to run on."""

    def __init__(self, extra: Preset | None = None) -> None:
        self._base = BuiltinCatalog.default()
        self._extra = extra

    def _extra_file(self, path: str) -> PresetFile | None:
        if self._extra is None:
            return None
        folded = collision_key(path)
        return next((file for file in self._extra.files if collision_key(file.path) == folded), None)

    def get(self, name: str) -> Preset | None:
        return self._base.get(name)

    def policy_for(self, path: str) -> PresetFile | None:
        if self._extra is not None and self._extra.name != "zsh":
            found = self._extra_file(path)
            if found is not None:
                return found
        return self._base.policy_for(path)

    def knows(self, preset: str, path: str) -> bool:
        if self._extra is not None and preset == self._extra.name:
            return self._extra_file(path) is not None
        return self._base.knows(preset, path)


KEYED_CATALOG = _Catalog(JSONC_PRESET)
TOML_CATALOG = _Catalog(TOML_PRESET)


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


def _git(repo: Path | str, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout


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


def _config(machine: Machine, items: tuple[LocalItem, ...]) -> SyncConfig:
    return SyncConfig.create(remote=machine.remote, machine_id=machine.machine_id, items=items)


def _zsh_config(machine: Machine) -> SyncConfig:
    return _config(machine, (LocalItem.create(path=ZSH, preset="zsh", scope="common", format="text"),))


def _jsonc_config(machine: Machine) -> SyncConfig:
    return _config(machine, (LocalItem.create(path=JSONC, preset="keyed-test", scope="common", format="jsonc"),))


def _toml_config(machine: Machine) -> SyncConfig:
    return _config(machine, (LocalItem.create(path=TOML, preset="toml-test", scope="common", format="toml"),))


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


def _run(machine: Machine, *, adopt: AdoptChoice | None = None, catalog: _Catalog | None = None) -> SyncReport:
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
        catalog=catalog if catalog is not None else KEYED_CATALOG,
        validators={"zsh": ZshSyntaxValidator()},
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


def _zsh_pair(tmp_path: Path, make_store, content: bytes = b"echo one\necho two\n") -> tuple[Machine, Machine]:
    """Two machines in sync on `content`, both tracking `.zshrc` under the zsh preset."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a, _zsh_config(a))
    _write(a, ZSH, content)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, _zsh_config(b), import_from=a.recovery)
    _run(b)
    return a, b


def _keyed_pair(tmp_path: Path, make_store, content: bytes) -> tuple[Machine, Machine]:
    """Two machines in sync on a JSONC settings file owned by the keyed test policy."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, content)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _run(b)
    return a, b


def _decrypted_objects(machine: Machine) -> list[bytes]:
    """Every object the machine's repo holds, decrypted — the check a leak cannot pass."""
    cipher = EnvelopeCipher(machine.store)
    files = FileRepo(machine.repo)
    return [
        cipher.open(files.read(rel) or b"", domain="object", file_id=Path(rel).stem)
        for rel in files.list_paths("objects")
    ]


# --------------------------------------------------------------------------- zsh


def test_a_zshrc_change_on_one_machine_reaches_the_other(tmp_path, memory_key_store) -> None:
    # given
    a, b = _zsh_pair(tmp_path, memory_key_store)

    # when
    _write(a, ZSH, b"echo changed\n")
    _run(a)
    _run(b)

    # then
    assert _read(b, ZSH) == b"echo changed\n"


def test_a_merge_that_would_not_be_valid_shell_is_a_conflict(tmp_path, memory_key_store) -> None:
    # given — each side changed a different line, far enough apart that the text merge succeeds
    base = b"echo a\necho b\necho c\necho d\necho e\n"
    a, b = _zsh_pair(tmp_path, memory_key_store, base)
    _write(a, ZSH, b"echo a\necho b\necho c\necho d\nfi\n")
    _run(a)
    _write(b, ZSH, b"then\necho b\necho c\necho d\necho e\n")

    # when — B merges A's published line with its own; the text merge has nothing to report
    report = _run(b)

    # then — the merged text would not run, so it is a conflict and nothing is applied
    assert report.has_conflicts is True
    assert any("syntax" in conflict.detail for conflict in report.conflicts)
    assert _read(b, ZSH) == b"then\necho b\necho c\necho d\necho e\n"


def test_an_invalid_zshrc_is_never_written_on_apply(tmp_path, memory_key_store) -> None:
    # given — A publishes a file with a syntax error
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _zsh_config(a))
    _write(a, ZSH, b"if true; then\n")
    _run(a)

    # when — a second machine must create the file and validates it first
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _zsh_config(b), import_from=a.recovery)
    report = _run(b)

    # then — reported as invalid, and the file was not created
    assert report.invalid == (ZSH,)
    assert (b.home / ZSH).exists() is False


def test_a_zshrc_that_is_a_symlink_is_reported_and_never_written(tmp_path, memory_key_store) -> None:
    # given — B's `.zshrc` is a link into a dotfiles tree, and A publishes a change
    a, b = _zsh_pair(tmp_path, memory_key_store)
    target = b.home / "dotfiles" / "zshrc"
    _write(b, "dotfiles/zshrc", b"echo one\necho two\n")
    (b.home / ZSH).unlink()
    (b.home / ZSH).symlink_to(target)
    _write(a, ZSH, b"echo changed\n")
    _run(a)

    # when
    report = _run(b)

    # then — reported, and neither the link nor its target is touched (§2.4)
    assert report.symlinks == (ZSH,)
    assert (b.home / ZSH).is_symlink() is True
    assert target.read_bytes() == b"echo one\necho two\n"


def test_a_record_the_local_catalog_does_not_own_is_refused(tmp_path, memory_key_store) -> None:
    # given — A tracks a path under the name `zsh` that the zsh preset does not own
    remote = _bare_remote(tmp_path)
    path = "notes/settings.txt"
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _config(a, (LocalItem.create(path=path, preset="zsh", scope="common", format="text"),)))
    _write(a, path, b"set\n")
    _run(a)

    # when — B tracks the same path, so only the catalog provenance can refuse it
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(
        b,
        _config(b, (LocalItem.create(path=path, preset="zsh", scope="common", format="text"),)),
        import_from=a.recovery,
    )
    report = _run(b)

    # then — the path is reported and never written (docs/design/03-manifest.md)
    assert report.refused == (path,)
    assert (b.home / path).exists() is False


# --------------------------------------------------------------------------- projection


def test_a_noise_only_change_produces_no_commit(tmp_path, memory_key_store) -> None:
    # given — two machines in sync on a settings file with a noise key
    base = b'{\n  "editor.fontSize": 13,\n  "window": {\n    "zoomLevel": 1\n  }\n}\n'
    a, _ = _keyed_pair(tmp_path, memory_key_store, base)
    before = _git(a.repo, "rev-list", "--count", "HEAD").strip()

    # when — only the noise key changes
    _write(a, JSONC, base.replace(b'"zoomLevel": 1', b'"zoomLevel": 5'))
    report = _run(a)

    # then — no diff, no commit, and the local value is left alone
    assert report.unchanged is True
    assert _git(a.repo, "rev-list", "--count", "HEAD").strip() == before
    assert b'"zoomLevel": 5' in _read(a, JSONC)


def test_an_excluded_key_never_reaches_a_repo_object(tmp_path, memory_key_store) -> None:
    # given — the tracked settings file carries a local proxy credential
    content = (
        b'{\n  "editor.fontSize": 13,\n  "http": {\n    "proxyAuthorization": "' + SECRET.encode() + b'"\n  }\n}\n'
    )

    # when
    a, _ = _keyed_pair(tmp_path, memory_key_store, content)
    plaintexts = _decrypted_objects(a)

    # then — the key was cut before sealing, so it is in no object at all (§3.4)
    assert plaintexts != []
    assert [blob for blob in plaintexts if SECRET.encode() in blob] == []
    assert [blob for blob in plaintexts if b"proxyAuthorization" in blob] == []
    assert _read(a, JSONC) == content


def test_apply_keeps_this_machines_excluded_value(tmp_path, memory_key_store) -> None:
    # given — A published a settings file; B's own file holds a different local credential
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, b'{\n  "editor.fontSize": 15,\n  "window": {\n    "zoomLevel": 1\n  }\n}\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(
        b,
        JSONC,
        b'{\n  "editor.fontSize": 13,\n  "http": {\n    "proxyAuthorization": "' + SECRET.encode() + b'"\n  }\n}\n',
    )

    # when — B takes the repo's side
    _run(b, adopt="remote")

    # then — the repo's setting and this machine's credential both survive
    merged = _read(b, JSONC)
    assert b'"editor.fontSize": 15' in merged
    assert SECRET.encode() in merged


def test_a_settings_file_the_policy_cannot_parse_is_reported_unprojectable(tmp_path, memory_key_store) -> None:
    # given — B's tracked file is malformed for its format
    a, b = _keyed_pair(tmp_path, memory_key_store, b'{\n  "editor.fontSize": 13\n}\n')
    _write(b, JSONC, b'{\n  "editor.fontSize": 13,\n')

    # when
    report = _run(b)

    # then — never sealed raw, and reported rather than silently skipped
    assert report.unprojectable == (JSONC,)
    assert _read(b, JSONC) == b'{\n  "editor.fontSize": 13,\n'
    assert a.home.exists() is True


def test_a_policy_whose_format_has_no_adapter_is_reported_unprojectable(tmp_path, memory_key_store) -> None:
    # given — the catalog owns a TOML file and this build has no TOML adapter yet
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _toml_config(machine))
    _write(machine, TOML, b'noise = "x"\n')

    # when
    report = _run(machine, catalog=TOML_CATALOG)

    # then — fail-closed: the raw file is never sealed
    assert report.unprojectable == (TOML,)
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_record_whose_format_disagrees_with_the_catalog_is_unprojectable(tmp_path, memory_key_store) -> None:
    # given — the local config declares JSONC for a path the catalog says is TOML
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    item = LocalItem.create(path=TOML, preset="toml-test", scope="common", format="jsonc")
    _initialize(machine, _config(machine, (item,)))
    _write(machine, TOML, b'noise = "x"\n')

    # when
    report = _run(machine, catalog=TOML_CATALOG)

    # then — a mismatch is refused, never guessed at
    assert report.unprojectable == (TOML,)
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_tracked_file_that_is_not_utf8_is_reported_unprojectable(tmp_path, memory_key_store) -> None:
    # given — a settings file with a stray byte, so no key path can be trusted
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _jsonc_config(machine))
    _write(machine, JSONC, b'\xff\xfe{\n  "editor.fontSize": 13\n}\n')

    # when
    report = _run(machine)

    # then
    assert report.unprojectable == (JSONC,)
    assert FileRepo(machine.repo).list_paths("objects") == ()
