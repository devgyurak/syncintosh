# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the built-in catalog, collect-side projection, and the zsh syntax gate.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
The catalog and the validator are the ones the CLI wires, so these run the real policy rather
than a stub. The built-in catalog has no keyed-format preset yet, so the JSONC cases add one
policy beside it — the machinery under test is the same one the VS Code preset will use.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.errors import ContentParseError, PolicyMismatch
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import FileFormat
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.models import Scope
from syncintosh.core.paths import collision_key
from syncintosh.core.policy import policy_digest
from syncintosh.core.ports import ContentAdapter, PresetCatalog
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, object_path
from syncintosh.core.tree import Tree
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.diff import diff_repo
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
# A second JSONC file so a machine can have a change of its own while another file changes remotely,
# which is what makes a merge re-use an object instead of merging content.
SECOND = "Library/Application Support/Code/User/keybindings.json"
SECOND_FILE = PresetFile.create(path=SECOND, format="jsonc")
JSONC_PRESET = Preset.create(name="keyed-test", files=(JSONC_POLICY, SECOND_FILE))
TOML_PRESET = Preset.create(name="toml-test", files=(PresetFile.create(path=TOML, format="toml", noise={("noise",)}),))


class _UnawareCatalog:
    """A catalog that owns nothing — a build whose presets do not include the path under test."""

    def policy_for(self, path: str) -> PresetFile | None:
        return None

    def knows(self, preset: str, path: str) -> bool:
        return True

    def get(self, name: str) -> Preset | None:
        return None

    def preset_for(self, path: str) -> Preset | None:
        return None

    def ignore_list_files(self) -> tuple:
        return ()

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        return None


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
        if self._extra is not None and name == self._extra.name:
            return self._extra
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

    def preset_for(self, path: str) -> Preset | None:
        if self._extra is not None and self._extra_file(path) is not None:
            return self._extra
        return self._base.preset_for(path)

    def ignore_list_files(self) -> tuple:
        return self._base.ignore_list_files()

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        if self._extra is not None and name == self._extra.name:
            return (policy_digest(self._extra),)
        return self._base.policy_history(name)


class _ClaimingZsh:
    """A build whose zsh preset also owns one extra path, under the shipped policy history.

    It stamps the very digest the default catalog expects, so a machine on the default catalog accepts its repo
    and the only disagreement left is ownership of the path.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._base = BuiltinCatalog.default()

    def _claims(self, path: str) -> bool:
        return collision_key(path) == collision_key(self._path)

    def policy_for(self, path: str) -> PresetFile | None:
        return PresetFile.create(path=self._path, format="text") if self._claims(path) else self._base.policy_for(path)

    def knows(self, preset: str, path: str) -> bool:
        return True if self._claims(path) else self._base.knows(preset, path)

    def get(self, name: str) -> Preset | None:
        return self._base.get(name)

    def preset_for(self, path: str) -> Preset | None:
        return self._base.get("zsh") if self._claims(path) else self._base.preset_for(path)

    def ignore_list_files(self) -> tuple:
        return self._base.ignore_list_files()

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        return self._base.policy_history(name)


class _Release(_Catalog):
    """A build that has shipped `releases` of one preset, oldest first, and enforces the last.

    Its history is the digest of every release, so a build with one more release reads a repo stamped by the
    shorter one as `older`, and the shorter reads the longer's as `newer` — which is how two builds of the same
    program are told apart without changing the program.
    """

    def __init__(self, *releases: Preset) -> None:
        super().__init__(releases[-1])
        self._releases = releases

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        if name == self._releases[-1].name:
            return tuple(policy_digest(release) for release in self._releases)
        return super().policy_history(name)


# Release 1 did not exclude the credential key; release 2 (the current `JSONC_POLICY`) does.
RELEASE_1 = Preset.create(
    name="keyed-test",
    files=(PresetFile.create(path=JSONC, format="jsonc", noise={("window", "zoomLevel")}), SECOND_FILE),
)
RELEASE_2 = JSONC_PRESET
# A fork that also shipped a second release, but a different one.
FORK_2 = Preset.create(
    name="keyed-test",
    files=(PresetFile.create(path=JSONC, format="jsonc", excluded={("telemetry",)}), SECOND_FILE),
)
OLD_BUILD = _Release(RELEASE_1)
NEW_BUILD = _Release(RELEASE_1, RELEASE_2)
FORK_BUILD = _Release(RELEASE_1, FORK_2)

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


def _record_for(machine: Machine, path: str):
    """The machine's published record for `path`, read from its own manifest."""
    files = FileRepo(machine.repo)
    manifest = loads_manifest(
        EnvelopeCipher(machine.store).open(files.read(MANIFEST_PATH) or b"", domain="manifest", file_id="")
    )
    return next(record for record in manifest.records.values() if record.path == path)


def _two_file_config(machine: Machine) -> SyncConfig:
    return _config(
        machine,
        (
            LocalItem.create(path=JSONC, preset="keyed-test", scope="common", format="jsonc"),
            LocalItem.create(path=SECOND, preset="keyed-test", scope="common", format="jsonc"),
        ),
    )


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


def _run(
    machine: Machine,
    *,
    adopt: AdoptChoice | None = None,
    catalog: PresetCatalog | None = None,
    adapters: Mapping[FileFormat, ContentAdapter] | None = None,
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
        adapters={"jsonc": JsoncAdapter()} if adapters is None else adapters,
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
    assert [refusal.path for refusal in report.invalid] == [ZSH]
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


def test_an_item_whose_preset_does_not_own_its_path_is_refused(tmp_path, memory_key_store) -> None:
    # given — the local config names a path under `zsh` that the zsh preset does not own. A fresh
    # record for it would be refused by every machine's apply, so this machine would never get a
    # ledger entry and would be asked for `--adopt` on every later sync.
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    path = "notes/settings.txt"
    _initialize(machine, _config(machine, (LocalItem.create(path=path, preset="zsh", scope="common", format="text"),)))
    _write(machine, path, b"set\n")

    # when
    report = _run(machine)

    # then — refused at collect with the reason, and nothing is published for it
    assert [refusal.path for refusal in report.refused] == [path]
    assert report.refused[0].reason == "the path's preset does not own it"
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_record_the_local_catalog_does_not_own_is_refused(tmp_path, memory_key_store) -> None:
    # given — A publishes the path because its own catalog does own it under the name `zsh`;
    # B's catalog does not, so B must refuse the record rather than write it
    remote = _bare_remote(tmp_path)
    path = "notes/settings.txt"
    owned = _ClaimingZsh(path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _config(a, (LocalItem.create(path=path, preset="zsh", scope="common", format="text"),)))
    _write(a, path, b"set\n")
    _run(a, catalog=owned)
    assert FileRepo(a.repo).list_paths("objects") != ()

    # when — B tracks the same path, so only the catalog provenance can refuse it
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(
        b,
        _config(b, (LocalItem.create(path=path, preset="zsh", scope="common", format="text"),)),
        import_from=a.recovery,
    )
    report = _run(b)

    # then — the path is reported and never written (docs/design/03-manifest.md)
    assert [refusal.path for refusal in report.refused] == [path]
    assert (b.home / path).exists() is False


def test_a_one_line_settings_file_never_wedges_a_machine(tmp_path, memory_key_store) -> None:
    # given — A published a one-line settings file, and B's own file carries the excluded key
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 13, "http": {"proxyAuthorization": "Basic FROM-B"}}\n')

    # when — B takes the repo's side, then A changes a shared setting and B syncs twice more
    first = _run(b, adopt="remote")
    _write(a, JSONC, b'{"editor.fontSize": 15}\n')
    _run(a)
    second = _run(b)
    third = _run(b)

    # then — nothing was ever unprojectable, the local credential survived, and the cycle settles
    # instead of reporting the file on every later sync
    assert first.unprojectable == ()
    assert second.unprojectable == ()
    assert b'"editor.fontSize": 15' in _read(b, JSONC)
    assert b"Basic FROM-B" in _read(b, JSONC)
    assert third.applied == ()
    assert third.unchanged is True


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
    assert [refusal.path for refusal in report.unprojectable] == [JSONC]
    assert _read(b, JSONC) == b'{\n  "editor.fontSize": 13,\n'
    assert a.home.exists() is True


def test_a_policy_whose_format_has_no_adapter_is_reported_unprojectable(tmp_path, memory_key_store) -> None:
    # given — the catalog owns a TOML file and this build has no TOML adapter yet
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _toml_config(machine))
    _write(machine, TOML, b'noise = "x"\n')

    # when
    report = _run(machine, catalog=TOML_CATALOG)

    # then — fail-closed (the raw file is never sealed), and the reason is reported rather than
    # left for the user to guess at
    assert [refusal.path for refusal in report.unprojectable] == [TOML]
    assert report.unprojectable[0].reason == "this build has no adapter for the file's format"
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_record_whose_format_disagrees_with_the_catalog_is_unprojectable(tmp_path, memory_key_store) -> None:
    # given — the local config declares JSONC for a path the catalog says is TOML
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    item = LocalItem.create(path=TOML, preset="toml-test", scope="common", format="jsonc")
    _initialize(machine, _config(machine, (item,)))
    _write(machine, TOML, b'noise = "x"\n')

    # when
    report = _run(machine, catalog=TOML_CATALOG)

    # then — a mismatch is refused, never guessed at, and named
    assert [refusal.path for refusal in report.unprojectable] == [TOML]
    assert report.unprojectable[0].reason == "the record's format is not the format the catalog uses"
    assert FileRepo(machine.repo).list_paths("objects") == ()


def test_a_tracked_file_that_is_not_utf8_is_reported_unprojectable(tmp_path, memory_key_store) -> None:
    # given — a settings file with a stray byte, so no key path can be trusted
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _jsonc_config(machine))
    _write(machine, JSONC, b'\xff\xfe{\n  "editor.fontSize": 13\n}\n')

    # when
    report = _run(machine)

    # then
    assert [refusal.path for refusal in report.unprojectable] == [JSONC]
    assert FileRepo(machine.repo).list_paths("objects") == ()


class _CountingAdapter:
    """A `ContentAdapter` that counts `parse` calls, so a cycle's work is observable."""

    def __init__(self, inner: ContentAdapter) -> None:
        self._inner = inner
        self.parses = 0

    def parse(self, text: str) -> Tree:
        self.parses += 1
        return self._inner.parse(text)

    def render(self, tree: Tree, template: str, arrays=None, verbatim=None) -> str:
        return self._inner.render(tree, template)


def test_a_cycle_projects_a_file_once(tmp_path, memory_key_store) -> None:
    # given — one tracked file whose collect needs a projection
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    _initialize(machine, _jsonc_config(machine))
    _write(machine, JSONC, b'{"editor.fontSize": 13, "window": {"zoomLevel": 1}}\n')
    counting = _CountingAdapter(JsoncAdapter())

    # when
    _run(machine, adapters={"jsonc": counting})

    # then — two for collect (the projection, and the re-parse that verifies it) and two for
    # settle, which projects the bytes it actually read so the ledger records what it saw. Apply
    # reuses collect's projection because the raw bytes are unchanged, saving the two it used to
    # spend; without that reuse this would be 6. One more reads the file's own ignore list, once per cycle.
    assert counting.parses == 5


def test_a_dangerous_path_is_reported_as_dangerous_not_as_unowned(tmp_path, memory_key_store) -> None:
    # given — a private key under a preset that does not own it, so both refusals apply
    machine = _machine(tmp_path, memory_key_store, "machine-a", _bare_remote(tmp_path))
    item = LocalItem.create(path=".ssh/id_rsa", preset="zsh", scope="common", format="text")
    _initialize(machine, _config(machine, (item,)))
    _write(machine, ".ssh/id_rsa", b"-----BEGIN " + b"PRIVATE KEY-----\n")

    # when
    report = _run(machine)

    # then — the deny-list reason wins, because it is the one the user needs to see
    assert report.skipped == (".ssh/id_rsa",)
    assert report.refused == ()


def test_a_record_published_in_another_format_is_kept_as_stored_and_refused(tmp_path, memory_key_store) -> None:
    # given — A's catalog does not know the path, so A publishes it as plain `text`; this machine's
    # catalog says it is JSONC and strips keys from it
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    item = LocalItem.create(path=JSONC, preset="keyed-test", scope="common", format="text")
    _initialize(a, _config(a, (item,)))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=_UnawareCatalog())
    assert FileRepo(a.repo).list_paths("objects") != ()
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 13}\n')

    # when — B's own file is byte-for-byte the one collect read, so collect's projection is cached
    report = _run(b, adopt="remote")

    # then — refused: reusing that projection across a format mismatch would apply the record here
    # and refuse it on a machine whose file had been touched in the same window, for the same input. The policy
    # gate keeps the record as stored instead of failing the cycle, and says so
    assert [refusal.path for refusal in report.unprojectable] == [JSONC]
    assert report.unprojectable[0].reason == "the record's format is not the format the catalog uses"
    assert any(note.startswith("policy not migrated: " + JSONC) for note in report.notes)


def test_a_record_whose_format_disagrees_with_a_path_that_strips_nothing_is_refused(tmp_path, memory_key_store) -> None:
    # given — A publishes `.zshrc` as JSONC; this machine's catalog says it is plain text and strips
    # nothing from it, so the format check has to run for that policy too
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    item = LocalItem.create(path=ZSH, preset="zsh", scope="common", format="jsonc")
    _initialize(a, _config(a, (item,)))
    _write(a, ZSH, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=_UnawareCatalog())
    assert FileRepo(a.repo).list_paths("objects") != ()
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _zsh_config(b), import_from=a.recovery)
    _write(b, ZSH, b"echo one\n")

    # when
    report = _run(b, adopt="remote")
    again = _run(b, adopt="remote")

    # then — refused rather than written: writing it would leave apply and settle disagreeing, and
    # the file would be rewritten on every later cycle without ever settling
    assert [refusal.path for refusal in report.unprojectable] == [ZSH]
    assert report.unprojectable[0].reason == "the record's format is not the format the catalog uses"
    assert _read(b, ZSH) == b"echo one\n"
    assert again.unprojectable == report.unprojectable


def test_a_record_is_republished_in_the_format_its_object_is_really_in(tmp_path, memory_key_store) -> None:
    # given — A publishes the path as `text` (a build whose catalog does not know it), and this
    # machine's catalog says it is JSONC and strips keys from it
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    item = LocalItem.create(path=JSONC, preset="keyed-test", scope="common", format="text")
    _initialize(a, _config(a, (item,)))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=_UnawareCatalog())
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 15}\n')

    # when — B keeps its own content, which seals the object in the catalog's format
    report = _run(b, adopt="local")
    published = loads_manifest(
        EnvelopeCipher(b.store).open(FileRepo(b.repo).read(MANIFEST_PATH) or b"", domain="manifest", file_id="")
    )
    formats = {record.path: record.format for record in published.records.values()}

    # then — the record names the format the object is in, so a later scan reads it as JSONC (where
    # `$` is a literal) rather than as a shell file, and apply no longer sees a mismatch
    assert report.unprojectable == ()
    assert formats[JSONC] == "jsonc"


def test_a_diverged_merge_of_two_files_that_need_no_projection_does_not_crash(tmp_path, memory_key_store) -> None:
    # given — each machine edits a different tracked file, and the one that changed remotely needs no
    # projection (its record has no strip list)
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _two_file_config(a))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _write(a, SECOND, b'[\n  {"key": "cmd+k"}\n]\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _two_file_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 13}\n')
    _write(b, SECOND, b'[\n  {"key": "cmd+k"}\n]\n')
    _run(b)
    _write(a, SECOND, b'[\n  {"key": "cmd+j"}\n]\n')
    _run(a)
    _write(b, JSONC, b'{"editor.fontSize": 15}\n')

    # when — B merges against A's commit, and the object it re-uses is SECOND
    report = _run(b)

    # then — the merge completes with both changes, and both machines converge
    assert report.has_conflicts is False
    _run(a)
    assert _read(b, SECOND) == b'[\n  {"key": "cmd+j"}\n]\n'
    assert _read(b, JSONC) == b'{"editor.fontSize": 15}\n'
    assert _read(a, JSONC) == b'{"editor.fontSize": 15}\n'


# --------------------------------------------------------------------------- carried ciphertext (issue #26)


def _git_blob_id(machine: Machine, commit: str, path: str) -> str:
    """The git blob ID of `path`'s object at `commit`: equal IDs mean the ciphertext was not rewritten."""
    return _git(machine.repo, "rev-parse", f"{commit}:{object_path(_record_for(machine, path).file_id)}").strip()


def test_a_merge_carries_the_ciphertext_of_an_object_it_only_reuses(tmp_path, memory_key_store) -> None:
    # given — A changes SECOND and B changes JSONC, so B's merge re-uses A's SECOND object unchanged
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _two_file_config(a))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _write(a, SECOND, b'[\n  {"key": "cmd+k"}\n]\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _two_file_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 13}\n')
    _write(b, SECOND, b'[\n  {"key": "cmd+k"}\n]\n')
    _run(b)
    _write(a, SECOND, b'[\n  {"key": "cmd+j"}\n]\n')
    _run(a)
    a_commit = _git(b.remote, "rev-parse", BRANCH).strip()
    _write(b, JSONC, b'{"editor.fontSize": 15}\n')

    # when
    _run(b)
    merge_commit = _git(b.repo, "rev-parse", "HEAD").strip()

    # then — the merge commit has A's SECOND blob byte for byte, and it is a merge of two parents
    assert len(_git(b.repo, "rev-list", "--parents", "-n", "1", merge_commit).split()) == 3
    assert _git_blob_id(b, merge_commit, SECOND) == _git_blob_id(b, a_commit, SECOND)
    assert _read(b, SECOND) == b'[\n  {"key": "cmd+j"}\n]\n'


def test_a_content_merge_that_equals_the_other_side_carries_its_ciphertext(tmp_path, memory_key_store) -> None:
    # given — both machines add the same line, and A also appends a blank line, so the merged text is A's exactly
    a, b = _zsh_pair(tmp_path, memory_key_store, b"echo one\n\necho two\n")
    _write(a, ZSH, b"echo one\necho x\n\necho two\n\n")
    _run(a)
    a_commit = _git(b.remote, "rev-parse", BRANCH).strip()
    _write(b, ZSH, b"echo one\necho x\n\necho two\n")

    # when
    _run(b)
    merge_commit = _git(b.repo, "rev-parse", "HEAD").strip()

    # then
    assert _read(b, ZSH) == b"echo one\necho x\n\necho two\n\n"
    assert _git_blob_id(b, merge_commit, ZSH) == _git_blob_id(b, a_commit, ZSH)


# --------------------------------------------------------------------------- the policy gate (issue #24)

CREDENTIAL = b'{"editor.fontSize": 13, "http": {"proxyAuthorization": "Basic FROM-A"}}\n'
OWN_CREDENTIAL = b'{"editor.fontSize": 13, "http": {"proxyAuthorization": "Basic FROM-B"}}\n'


def _manifest_at_head(machine: Machine):
    blob = GitCLITransport(machine.repo).read_blob(GitCLITransport(machine.repo).head() or "", MANIFEST_PATH)
    return loads_manifest(EnvelopeCipher(machine.store).open(blob or b"", domain="manifest", file_id=""))


def _published_by_the_old_build(tmp_path, make_store) -> tuple[Machine, Machine]:
    """A (the old build) has published a settings file that holds the credential key; B is cloned, not yet synced."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, CREDENTIAL)
    _run(a, catalog=OLD_BUILD)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    return a, b


def test_a_newer_build_migrates_an_older_repo_and_keeps_its_own_values(tmp_path, memory_key_store) -> None:
    # given — the old build published the credential key; B's file has its own value for it
    _, b = _published_by_the_old_build(tmp_path, memory_key_store)
    _write(b, JSONC, OWN_CREDENTIAL)

    # when
    report = _run(b, adopt="remote", catalog=NEW_BUILD)
    plaintexts = _decrypted_objects(b)

    # then — the repo no longer holds the key, B's live value is untouched, and the stamp says release 2
    assert [blob for blob in plaintexts if b"proxyAuthorization" in blob] == []
    assert [blob for blob in plaintexts if b"FROM-A" in blob] == []
    assert b"Basic FROM-B" in _read(b, JSONC)
    assert _manifest_at_head(b).policy["keyed-test"].rev == 2
    assert "policy migrated: keyed-test rev 1→2" in report.notes


def test_a_migrated_object_is_sealed_once_and_collect_commits_the_gates_blob(
    tmp_path, memory_key_store, monkeypatch
) -> None:
    # given — the old build published the credential key, and every object seal is recorded from here on
    _, b = _published_by_the_old_build(tmp_path, memory_key_store)
    sealed: list[tuple[str, bytes]] = []
    real_seal = EnvelopeCipher.seal

    def recording_seal(self, plaintext, *, domain, file_id, key_id):
        blob = real_seal(self, plaintext, domain=domain, file_id=file_id, key_id=key_id)
        sealed.append((domain + ":" + file_id, blob))
        return blob

    monkeypatch.setattr(EnvelopeCipher, "seal", recording_seal)

    # when
    _run(b, adopt="remote", catalog=NEW_BUILD)
    file_id = _record_for(b, JSONC).file_id
    committed = GitCLITransport(b.repo).read_blob(_git(b.repo, "rev-parse", "HEAD").strip(), object_path(file_id))

    # then — the gate sealed the migrated object, and the committed object is that very blob
    assert [blob for subject, blob in sealed if subject == "object:" + file_id] == [committed]


def test_an_older_build_stops_on_a_repo_a_newer_build_migrated(tmp_path, memory_key_store) -> None:
    # given — B migrated the repo; A (the old build) has an edit of its own
    a, b = _published_by_the_old_build(tmp_path, memory_key_store)
    _run(b, adopt="remote", catalog=NEW_BUILD)
    published = _git(b.remote, "rev-parse", BRANCH)
    edited = CREDENTIAL.replace(b"13", b"14")
    _write(a, JSONC, edited)

    # when & then — stopped after the fetch: nothing was merged, pushed, or applied
    with pytest.raises(PolicyMismatch, match="upgrade syncintosh on this Mac"):
        _run(a, catalog=OLD_BUILD)
    assert _git(a.remote, "rev-parse", BRANCH) == published
    assert _read(a, JSONC) == edited


def test_a_diff_warns_and_still_shows_what_it_can_when_the_repo_is_newer(tmp_path, memory_key_store) -> None:
    # given — the repo was migrated by a newer build, and the old build has fetched it before stopping
    a, b = _published_by_the_old_build(tmp_path, memory_key_store)
    _run(b, adopt="remote", catalog=NEW_BUILD)
    with pytest.raises(PolicyMismatch):
        _run(a, catalog=OLD_BUILD)

    # when
    report = diff_repo(
        transport=GitCLITransport(a.repo),
        cipher=EnvelopeCipher(a.store),
        live=SystemLiveFiles(),
        config=load_config(a.config_file),
        home=str(a.home),
        deny=SystemDenyList(),
        branch=BRANCH,
        catalog=OLD_BUILD,
        adapters={"jsonc": JsoncAdapter()},
        validators={},
    )

    # then — read-only, with the warning
    assert [entry.path for entry in report.entries] == [JSONC]
    assert len(report.policy_warnings) == 1
    assert "upgrade syncintosh on this Mac" in report.policy_warnings[0]


def test_a_stamp_this_build_has_never_shipped_stops_the_cycle(tmp_path, memory_key_store) -> None:
    # given — release 2 here is not the fork's release 2
    a, b = _published_by_the_old_build(tmp_path, memory_key_store)
    _run(b, adopt="remote", catalog=NEW_BUILD)

    # when & then
    with pytest.raises(PolicyMismatch, match="matches no policy this build has shipped"):
        _run(a, catalog=FORK_BUILD)


def test_a_preset_this_build_lacks_stops_the_cycle_before_anything_is_collected(tmp_path, memory_key_store) -> None:
    # given — A's repo is stamped for a preset the catalog does not ship, and A has a live edit
    a, _ = _published_by_the_old_build(tmp_path, memory_key_store)
    before = GitCLITransport(a.repo).head()
    _write(a, JSONC, CREDENTIAL.replace(b"13", b"14"))

    # when & then — HEAD is judged first, so no collect commit is made
    with pytest.raises(PolicyMismatch):
        _run(a, catalog=_UnawareCatalog())
    assert GitCLITransport(a.repo).head() == before


def test_a_merge_takes_the_migrated_view_of_a_remote_the_old_build_wrote(tmp_path, memory_key_store) -> None:
    # given — B is cloned at the old build's first commit, which the old build then moves past (it holds the key)
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=OLD_BUILD)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(a, JSONC, CREDENTIAL)
    _run(a, catalog=OLD_BUILD)
    _write(b, JSONC, b'{"editor.fontSize": 13}\n')

    # when — B's collect commit (the migrated first commit) and the old build's second commit diverge
    report = _run(b, adopt="remote", catalog=NEW_BUILD)
    plaintexts = _decrypted_objects(b)

    # then — the merge read the old build's object through the migration, so the key is in no object
    assert report.has_conflicts is False
    assert [blob for blob in plaintexts if b"proxyAuthorization" in blob] == []
    assert [blob for blob in plaintexts if b"FROM-A" in blob] == []
    assert _manifest_at_head(b).policy["keyed-test"].rev == 2
    assert b"FROM-A" not in _read(b, JSONC)


def test_an_unstamped_object_a_build_did_not_project_is_migrated_not_taken_as_matching(
    tmp_path, memory_key_store
) -> None:
    # given — A's catalog does not own the path, so it sealed the credential with no projection and no stamp; B
    # holds a one-release history, which used to read a missing stamp as that release and let the object through
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, CREDENTIAL)
    _run(a, catalog=_UnawareCatalog())
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, OWN_CREDENTIAL)

    # when
    report = _run(b, adopt="remote", catalog=KEYED_CATALOG)

    # then — the repo no longer holds the key, and the preset is stamped because it was brought to the policy
    assert [blob for blob in _decrypted_objects(b) if b"FROM-A" in blob] == []
    assert _manifest_at_head(b).policy["keyed-test"].rev == 1
    assert "policy migrated: keyed-test unstamped→1" in report.notes


def _published_unowned(tmp_path, make_store, *, scope: Scope):
    """A (a build whose catalog does not own the path) has published it as `text`; B tracks nothing."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    owner = a.machine_id if scope == "machine" else None
    item = LocalItem.create(path=JSONC, preset="keyed-test", scope=scope, machine=owner, format="text")
    _initialize(a, _config(a, (item,)))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=_UnawareCatalog())
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, _config(b, ()), import_from=a.recovery)
    return a, b


@pytest.mark.parametrize("scope", ["common", "machine"])
def test_a_record_the_gate_cannot_reduce_is_kept_not_deleted_by_a_machine_that_does_not_collect_it(
    tmp_path, memory_key_store, scope
) -> None:
    # given — B tracks no such file, so nothing on B replaces the record the gate could not migrate
    a, b = _published_unowned(tmp_path, memory_key_store, scope=scope)
    before = {(r.file_id, r.scope, r.machine, r.format) for r in _manifest_at_head(a).records.values()}

    # when
    report = _run(b, catalog=KEYED_CATALOG)

    # then — the record is still there, with the same file ID
    after = {(r.file_id, r.scope, r.machine, r.format) for r in _manifest_at_head(b).records.values()}
    assert after == before
    assert any(note.startswith("policy not migrated: " + JSONC) for note in report.notes)


# Two releases of a preset whose second file strips a key too, so projecting it parses it (SECOND_FILE strips nothing).
NOISY_SECOND = PresetFile.create(path=SECOND, format="jsonc", noise={("window", "zoomLevel")})
MARKED_1 = Preset.create(
    name="keyed-test",
    files=(PresetFile.create(path=JSONC, format="jsonc", noise={("window", "zoomLevel")}), NOISY_SECOND),
)
MARKED_2 = Preset.create(name="keyed-test", files=(JSONC_POLICY, NOISY_SECOND))
MARKED_OLD = _Release(MARKED_1)
MARKED_NEW = _Release(MARKED_1, MARKED_2)


class _RefusesMarked:
    """A `ContentAdapter` that cannot parse text holding a marker, so one file of a preset will not project."""

    MARKER = "UNPARSEABLE"

    def __init__(self, inner: ContentAdapter) -> None:
        self._inner = inner

    def parse(self, text: str) -> Tree:
        if self.MARKER in text:
            raise ContentParseError("the text holds the marker")
        return self._inner.parse(text)

    def render(self, tree: Tree, template: str, arrays=None, verbatim=None) -> str:
        return self._inner.render(tree, template)


def test_a_stamp_is_never_held_back_so_an_older_build_still_stops(tmp_path, memory_key_store) -> None:
    # given — release 2 strips the credential from one file and cannot project the other (it has a marker the
    # new build's adapter will not parse), both under one preset and so one stamp
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _two_file_config(a))
    _write(a, JSONC, CREDENTIAL)
    _write(a, SECOND, b'{"editor.fontSize": 13, "x": "UNPARSEABLE"}\n')
    _run(a, catalog=MARKED_OLD)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _two_file_config(b), import_from=a.recovery)
    _write(b, JSONC, OWN_CREDENTIAL)
    _write(b, SECOND, b'{"editor.fontSize": 13}\n')
    migrated = _run(b, adopt="remote", catalog=MARKED_NEW, adapters={"jsonc": _RefusesMarked(JsoncAdapter())})
    edited = CREDENTIAL.replace(b"13", b"14")
    _write(a, JSONC, edited)

    # when & then — the preset is stamped with the new release whatever one of its files looked like, so the
    # older build stops rather than strip the credential it still keeps live
    assert any(note.startswith("policy not migrated: " + SECOND) for note in migrated.notes)
    assert _manifest_at_head(b).policy["keyed-test"].rev == 2
    with pytest.raises(PolicyMismatch):
        _run(a, catalog=MARKED_OLD)
    assert _read(a, JSONC) == edited


def test_apply_never_writes_a_stored_object_the_local_policy_would_change_to_a_new_file(
    tmp_path, memory_key_store
) -> None:
    # given — the repo holds, under a current stamp, an object the new build cannot project; C has no such file
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _two_file_config(a))
    _write(a, JSONC, CREDENTIAL)
    _write(a, SECOND, b'{"editor.fontSize": 13, "x": "UNPARSEABLE"}\n')
    _run(a, catalog=MARKED_OLD)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _two_file_config(b), import_from=a.recovery)
    _write(b, JSONC, OWN_CREDENTIAL)
    _write(b, SECOND, b'{"editor.fontSize": 13}\n')
    refuses: Mapping[FileFormat, ContentAdapter] = {"jsonc": _RefusesMarked(JsoncAdapter())}
    _run(b, adopt="remote", catalog=MARKED_NEW, adapters=refuses)
    c = _machine(tmp_path, memory_key_store, "machine-c", remote)
    _initialize(c, _two_file_config(c), import_from=a.recovery)
    (c.home / SECOND).parent.mkdir(parents=True)

    # when
    report = _run(c, catalog=MARKED_NEW, adapters=refuses)

    # then — the unreducible object is refused although nothing is on disk to compare it with
    assert SECOND in [refusal.path for refusal in report.unprojectable]
    assert not (c.home / SECOND).exists()


def test_a_projected_object_is_written_to_a_machine_that_has_no_such_file(tmp_path, memory_key_store) -> None:
    # given — B migrated the repo, so its objects are the local policy's projection; C has the directory only
    a, b = _published_by_the_old_build(tmp_path, memory_key_store)
    _run(b, adopt="remote", catalog=NEW_BUILD)
    c = _machine(tmp_path, memory_key_store, "machine-c", b.remote)
    _initialize(c, _jsonc_config(c), import_from=a.recovery)
    (c.home / JSONC).parent.mkdir(parents=True)

    # when
    report = _run(c, catalog=NEW_BUILD)

    # then — the stripped-form check does not refuse an object that is already its own projection
    assert (report.unprojectable, b"proxyAuthorization" in _read(c, JSONC)) == ((), False)
    assert JSONC in report.applied


class _StaleCatalog(_Release):
    """A build that stamps release 2 but projects with release 1's files: a stamp that over-claims."""

    def policy_for(self, path: str) -> PresetFile | None:
        return next((file for file in RELEASE_1.files if file.path == path), None)


def test_apply_refuses_an_unstripped_object_under_a_current_stamp_on_a_machine_with_no_file(
    tmp_path, memory_key_store
) -> None:
    # given — the stamp says release 2 but the object still holds the credential (a stamp is a claim, not a proof)
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, CREDENTIAL)
    _run(a, catalog=_StaleCatalog(RELEASE_1, RELEASE_2))
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    (b.home / JSONC).parent.mkdir(parents=True)

    # when
    report = _run(b, catalog=NEW_BUILD)

    # then — refused rather than written, although there is no live file to compare it with
    assert [(refusal.path, refusal.reason) for refusal in report.unprojectable] == [
        (JSONC, "the stored content could not be reduced to this machine's synced form")
    ]
    assert not (b.home / JSONC).exists()


def test_apply_refuses_an_unstripped_object_under_a_current_stamp_when_a_live_file_exists(
    tmp_path, memory_key_store
) -> None:
    # given — the same over-claiming stamp, but this machine has a file of its own to re-attach keys to
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, CREDENTIAL)
    _run(a, catalog=_StaleCatalog(RELEASE_1, RELEASE_2))
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, OWN_CREDENTIAL)

    # when
    report = _run(b, adopt="remote", catalog=NEW_BUILD)

    # then — the restore round trip cannot give the object back, so it is refused and the file is untouched
    assert [(refusal.path, refusal.reason) for refusal in report.unprojectable] == [
        (JSONC, "this machine's own keys could not be re-attached to the file")
    ]
    assert _read(b, JSONC) == OWN_CREDENTIAL
