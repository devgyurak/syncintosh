# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the Ghostty preset — a validated text config, repeated keys, themes, and deferral.

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1).
The validator is a fake that rejects a marker: CI has no Ghostty, and the real app is never run. The fixture
carries real Ghostty key names with invented values. The catalog is the one the CLI wires.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core import presets as presets_core
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.config import LocalGlob, SyncConfig
from syncintosh.core.errors import SecretScanError, SyntaxInvalid
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.ports import AppDetector
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.core.resolution import Decide
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.ghostty import CONFIG_FILES, GHOSTTY, THEMES
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
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
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ghostty"
CONFIG = CONFIG_FILES[0]
BAD = "INVALID-MARKER"


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


class _FakeGhostty:
    """Rejects text carrying `BAD`, the way the real checker rejects a line it cannot parse."""

    def check(self, text: str, *, subject: str) -> None:
        if BAD in text:
            raise SyntaxInvalid(f"{subject}: ghostty reported an invalid config")


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
    # The shipped preset, so the config carries the four exact files and the glob — the
    # same thing `init` writes.
    return new_machine_config(
        remote=machine.remote,
        machine_id=machine.machine_id,
        preset_names=("ghostty",),
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
    catalog: BuiltinCatalog | None = None,
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
        adapters={},
        validators={"ghostty": _FakeGhostty()},
        catalog=catalog or BuiltinCatalog.default(),
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


def _pair(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """Two machines in sync on the fixture config; Ghostty's directory exists on both."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _write(a, CONFIG, _fixture("config"))
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    (b.home / ".config" / "ghostty").mkdir(parents=True)
    _run(b)
    return a, b


def _keybinds(data: bytes) -> list[str]:
    return [line for line in data.decode("utf-8").splitlines() if line.startswith("keybind")]


FIRST_BINDING = "keybind = cmd+t=new_tab"
LAST_BINDING = "keybind = cmd+shift+comma=reload_config"


def test_two_machines_converge_on_the_same_config(tmp_path, memory_key_store) -> None:
    # when
    a, b = _pair(tmp_path, memory_key_store)

    # then
    assert _read(b, CONFIG) == _read(a, CONFIG)


def test_a_merge_of_two_keybind_changes_keeps_every_line_and_its_order(tmp_path, memory_key_store) -> None:
    # given — A inserts a binding near the top of the list, B appends one at the end
    a, b = _pair(tmp_path, memory_key_store)
    base = _read(a, CONFIG)
    _write(a, CONFIG, base.replace(FIRST_BINDING.encode(), FIRST_BINDING.encode() + b"\nkeybind = cmd+n=new_window"))
    _write(
        b, CONFIG, base.replace(LAST_BINDING.encode(), LAST_BINDING.encode() + b"\nkeybind = cmd+j=jump_to_prompt:1")
    )
    _run(a)

    # when
    report = _run(b)

    # then — all fourteen original lines, in the original order, plus both new ones where each was put
    original = _keybinds(base)
    expected = [
        *original[:1],
        "keybind = cmd+n=new_window",
        *original[1:],
        "keybind = cmd+j=jump_to_prompt:1",
    ]
    assert (report.has_conflicts, _keybinds(_read(b, CONFIG))) == (False, expected)


def test_a_merge_that_fails_validation_is_not_written_and_stays_a_conflict(tmp_path, memory_key_store) -> None:
    # given — A publishes a valid change; B has a different, non-overlapping change that the checker rejects
    a, b = _pair(tmp_path, memory_key_store)
    base = _read(a, CONFIG)
    _write(a, CONFIG, base.replace(b"font-size = 13", b"font-size = 15"))
    _run(a)
    broken = base.replace(b"window-save-state = always", b"window-save-state = " + BAD.encode())
    _write(b, CONFIG, broken)

    # when
    report = _run(b)

    # then — the merge result is not applied: B's file is untouched, and the conflict is the validator's, not a
    # line clash (the two changes are far apart)
    assert ([each.detail for each in report.conflicts], _read(b, CONFIG)) == (
        ["the merged result would not pass its syntax check"],
        broken,
    )


def test_a_config_ghostty_would_reject_is_refused_on_apply(tmp_path, memory_key_store) -> None:
    # given — the remote carries a config the checker rejects (a machine whose validator is stricter)
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, CONFIG, _read(a, CONFIG).replace(b"font-size = 13", b"font-size = " + BAD.encode()))
    _run(a)
    c = _machine(tmp_path, memory_key_store, "machine-c", a.remote)
    _initialize(c, import_from=a.recovery)
    (c.home / ".config" / "ghostty").mkdir(parents=True)

    # when
    report = _run(c)

    # then — a refusal, and nothing was written to a live path
    assert ([each.path for each in report.invalid], (c.home / CONFIG).exists()) == ([CONFIG], False)


def test_a_theme_under_the_glob_is_collected_and_applied(tmp_path, memory_key_store) -> None:
    # given — A has a theme beside its config
    a, _ = _pair(tmp_path, memory_key_store)
    theme = THEMES.replace("*", "fixture-dark")
    _write(a, theme, b"background = #101010\nforeground = #e0e0e0\npalette = 0=#101010\n")
    _run(a)
    b = _machine(tmp_path, memory_key_store, "machine-theme", a.remote)
    _initialize(b, import_from=a.recovery)
    (b.home / ".config" / "ghostty" / "themes").mkdir(parents=True)

    # when
    report = _run(b)

    # then
    assert (theme in report.applied, _read(b, theme)) == (True, _read(a, theme))


def test_a_machine_without_ghostty_defers_with_the_download_page(tmp_path, memory_key_store) -> None:
    # given — A published; B has no Ghostty and no live file, and the detector says it is not installed
    a, _ = _pair(tmp_path, memory_key_store)
    b = _machine(tmp_path, memory_key_store, "machine-uninstalled", a.remote)
    _initialize(b, import_from=a.recovery)
    apps = _StubApps(False)

    # when
    report = _run(b, apps=apps)

    # then
    # (the other tracked paths are only "not on this machine yet": no file, and nothing to say about the app)
    missing_app = [each for each in report.deferred if each.download_url is not None]
    assert [(each.path, each.download_url, each.cask) for each in missing_app] == [
        (CONFIG, "https://ghostty.org/download", "ghostty")
    ]
    assert (b.home / ".config" / "ghostty").exists() is False
    assert apps.queried == ["com.mitchellh.ghostty"]


def test_a_credential_on_an_env_line_is_stopped_by_the_scan(tmp_path, memory_key_store) -> None:
    # given — `env` is where a launched program's token would sit; the scan is the only gate
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, CONFIG, _read(a, CONFIG) + b"env = GITHUB_TOKEN=ghp_" + b"A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n")

    # when
    with pytest.raises(SecretScanError):
        _run(a)

    # then — nothing carrying the token reached an object
    assert [blob for blob in _decrypted_objects(a) if b"ghp_A1b2" in blob] == []


HIDDEN_THEME_FILES = [
    pytest.param(".DS_Store", b"\x00\x00\x00\x01Bud1\xff\xfe", id="finder-metadata"),
    pytest.param(".fixture-dark.0123456789abcdef.syncintosh-check", b"background = #101010\n", id="killed-check-file"),
]


@pytest.mark.parametrize(("name", "data"), HIDDEN_THEME_FILES)
def test_a_hidden_file_in_the_themes_folder_is_never_collected(tmp_path, memory_key_store, name, data) -> None:
    # given — Finder (or a killed check) left a hidden file beside a real theme
    a, _ = _pair(tmp_path, memory_key_store)
    theme = THEMES.replace("*", "fixture-dark")
    _write(a, theme, b"background = #101010\n")
    _write(a, THEMES.replace("*", name), data)

    # when
    report = _run(a)

    # then — only the theme is published; the hidden file is not a record, so it cannot reach another Mac
    assert (report.created, report.refused) == ((theme,), ())


OLD_HIDDEN = ".config/ghostty/themes/.old-theme"


class OlderGlobRule:
    """Switches the glob rule between the one an older build had (a hidden name matches) and the current one.

    While `on`, every glob built from here on — a machine's config rebuilds its own from the path, and `catalog` is
    a fresh copy of the shipped preset — is built without the hidden-name rule. Nothing shared is modified, so the
    shipped catalog the rest of the suite uses is untouched.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.catalog = BuiltinCatalog.default()

    def on(self) -> None:
        self._monkeypatch.setattr(presets_core, "_skip_hidden", lambda name, pattern: pattern)
        files = tuple(
            PresetFile.create(path=each.path, format=each.format, validator=each.validator) for each in GHOSTTY.files
        )
        older = Preset.create(
            name=GHOSTTY.name,
            files=files,
            bundle_id=GHOSTTY.bundle_id,
            download_url=GHOSTTY.download_url,
            cask=GHOSTTY.cask,
        )
        self.catalog = BuiltinCatalog.create((older,))

    def off(self) -> None:
        self._monkeypatch.undo()
        self.catalog = BuiltinCatalog.default()


@pytest.fixture
def older_glob_rule(monkeypatch: pytest.MonkeyPatch) -> OlderGlobRule:
    return OlderGlobRule(monkeypatch)


def test_a_hidden_record_an_older_build_published_is_refused_not_deleted(
    tmp_path, memory_key_store, older_glob_rule
) -> None:
    # given — under the older rule `themes/*` took a hidden theme in, and the repo holds its record
    older_glob_rule.on()
    a, _ = _pair(tmp_path, memory_key_store)
    _write(a, OLD_HIDDEN, b"background = #101010\n")
    _run(a, catalog=older_glob_rule.catalog)
    older_glob_rule.off()
    save_config(_config(a), a.config_file)
    b = _machine(tmp_path, memory_key_store, "machine-b2", a.remote)
    _initialize(b, import_from=a.recovery)
    (b.home / ".config" / "ghostty" / "themes").mkdir(parents=True)

    # when — both Macs sync under the current rule
    on_a, on_b = _run(a), _run(b)

    # then — the record is reported as one the preset does not own (blocking, the existing rule for such a record),
    # nothing is deleted from the repo or from the Mac that has the file, and nothing is written to the other
    assert [(each.path, each.reason, each.blocking) for each in (*on_a.refused, *on_b.refused)] == [
        (OLD_HIDDEN, "the path's preset does not own it", True)
    ] * 2
    assert ((a.home / OLD_HIDDEN).exists(), (b.home / OLD_HIDDEN).exists()) == (True, False)


def _user_glob_config(machine: Machine) -> SyncConfig:
    """A machine's own entry for a folder of its own: every file in `dots/`, under a preset name of the user's."""
    return SyncConfig.create(
        remote=machine.remote,
        machine_id=machine.machine_id,
        items=(),
        globs=(LocalGlob.create(path="dots/*", preset="mine", scope="common", format="text"),),
    )


def test_a_hidden_file_a_wildcard_glob_used_to_track_is_kept_not_deleted(
    tmp_path, memory_key_store, older_glob_rule
) -> None:
    # given — under the older rule `dots/*` tracked a hidden file and a plain one, and both are published
    older_glob_rule.on()
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    save_config(_user_glob_config(a), a.config_file)
    _write(a, "dots/.rc", b"export A=1\n")
    _write(a, "dots/plain", b"x\n")
    _run(a)
    older_glob_rule.off()
    save_config(_user_glob_config(a), a.config_file)
    b = _machine(tmp_path, memory_key_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    save_config(_user_glob_config(b), b.config_file)
    (b.home / "dots").mkdir(parents=True)

    # when — both Macs sync under the current rule, and A once more to see what the first sync left in the repo
    on_a, on_b, again = _run(a), _run(b), _run(a)

    # then — the hidden file leaves the glob quietly (carried, never a blocking refusal), the repo still holds its
    # record (it is reported again), nothing is deleted from A, nothing is written to B, and the plain file syncs
    quiet = [("dots/.rc", "the path is not in this machine's config", False)]
    assert [[(each.path, each.reason, each.blocking) for each in report.refused] for report in (on_a, on_b, again)] == [
        quiet,
        quiet,
        quiet,
    ]
    assert ((a.home / "dots/.rc").exists(), (b.home / "dots/.rc").exists(), (b.home / "dots/plain").exists()) == (
        True,
        False,
        True,
    )
