# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the built-in catalog, collect-side projection, and the zsh syntax gate.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
The catalog and the validator are the ones the CLI wires, so these run the real policy rather
than a stub. The built-in catalog has no keyed-format preset yet, so the JSONC cases add one
policy beside it — the machinery under test is the same one the VS Code preset will use.
"""

from __future__ import annotations

import hashlib
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
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.manifest import FileFormat
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import ContentAdapter, PresetCatalog
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, object_path
from syncintosh.core.tree import Tree
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
    return sync(
        transport=active,
        files=FileRepo(machine.repo),
        cipher=EnvelopeCipher(machine.store),
        scanner=PatternSecretScanner(),
        store=machine.store,
        codec=machine.codec,
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
    owned = _Catalog(Preset.create(name="zsh", files=(PresetFile.create(path=path, format="text"),)))
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

    def render(self, tree: Tree, template: str) -> str:
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
    # spend; without that reuse this would be 6.
    assert counting.parses == 4


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


def test_a_record_published_in_another_format_is_refused_however_fresh_the_file_is(tmp_path, memory_key_store) -> None:
    # given — A's catalog does not know the path, so A publishes it as plain `text`; this machine's
    # catalog says it is JSONC and strips keys from it
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    item = LocalItem.create(path=JSONC, preset="keyed-test", scope="common", format="text")
    _initialize(a, _config(a, (item,)))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=_Catalog())
    assert FileRepo(a.repo).list_paths("objects") != ()
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 13}\n')

    # when — B's own file is byte-for-byte the one collect read, so collect's projection is cached
    report = _run(b, adopt="remote")

    # then — refused: reusing that projection across a format mismatch would apply the record here
    # and refuse it on a machine whose file had been touched in the same window, for the same input
    assert [refusal.path for refusal in report.unprojectable] == [JSONC]
    assert report.unprojectable[0].reason == "the record's format is not the format the catalog uses"


def test_a_merge_never_re_seals_a_credential_another_build_published(tmp_path, memory_key_store) -> None:
    # given — B applied a version without the credential, so it has a ledger baseline, and A (a
    # build whose preset does not exclude the key) then published one that holds it
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _jsonc_config(a))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _run(a, catalog=_UnawareCatalog())
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _jsonc_config(b), import_from=a.recovery)
    _write(b, JSONC, b'{"editor.fontSize": 13, "http": {"proxyAuthorization": "Basic FROM-B"}}\n')
    _run(b, adopt="remote")
    _write(a, JSONC, b'{"editor.fontSize": 13, "http": {"proxyAuthorization": "Basic FROM-A"}}\n')
    _run(a, catalog=_UnawareCatalog())

    # when — B edits its own file and merges against HEAD, which still holds A's credential
    _write(b, JSONC, b'{"editor.fontSize": 13, "editor.tabSize": 2, "http": {"proxyAuthorization": "Basic FROM-B"}}\n')
    report = _run(b)
    plaintexts = _decrypted_objects(b)

    # then — the merge result was projected before it was sealed, so neither machine's credential is
    # in any object, and B's own file kept its value
    assert report.has_conflicts is False
    assert report.unprojectable == ()
    assert [blob for blob in plaintexts if b"proxyAuthorization" in blob] == []
    assert [blob for blob in plaintexts if b"FROM-A" in blob] == []
    assert [blob for blob in plaintexts if b"FROM-B" in blob] == []
    assert b"Basic FROM-B" in _read(b, JSONC)
    assert b'"editor.tabSize": 2' in _read(b, JSONC)


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


def test_a_merge_projects_an_object_it_only_re_uses(tmp_path, memory_key_store) -> None:
    # given — A (a build that does not exclude the key) changes `settings.json` while B changes its
    # *other* file, so the merge re-uses A's settings object instead of merging its content
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a, _two_file_config(a))
    _write(a, JSONC, b'{"editor.fontSize": 13}\n')
    _write(a, SECOND, b'[\n  {\n    "key": "cmd+k"\n  }\n]\n')
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, _two_file_config(b), import_from=a.recovery)
    _run(b, adopt="remote")
    _write(a, JSONC, b'{"editor.fontSize": 13, "http": {"proxyAuthorization": "Basic FROM-A"}}\n')
    _run(a, catalog=_UnawareCatalog())
    _write(b, SECOND, b'[\n  {\n    "key": "cmd+j"\n  }\n]\n')

    # when
    report = _run(b)
    head = GitCLITransport(b.repo).head() or ""
    files = FileRepo(b.repo)
    manifest = loads_manifest(
        EnvelopeCipher(b.store).open(files.read(MANIFEST_PATH) or b"", domain="manifest", file_id="")
    )
    record = next(item for item in manifest.records.values() if item.path == JSONC)
    sealed = EnvelopeCipher(b.store).open(
        files.read(object_path(record.file_id)) or b"", domain="object", file_id=record.file_id
    )

    # then — the object the merge commit names was projected before it was sealed, so the key this
    # machine's policy strips is not in the commit it pushed
    assert report.has_conflicts is False
    assert b"proxyAuthorization" not in sealed
    assert b"FROM-A" not in sealed
    assert head != ""
    assert record.sha256 == hashlib.sha256(sealed).hexdigest()


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
