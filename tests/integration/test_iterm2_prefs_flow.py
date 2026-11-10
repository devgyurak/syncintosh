# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the iTerm2 preferences domain, synced between two Macs through `defaults` (issue #58).

A temp bare remote, two temp HOMEs, in-memory key rings and no network (``AGENTS.md`` §5.1). The domain is served by
the `fake_defaults` executable (`tests/conftest.py`), which keeps each Mac's domain as a plist under its own temp
HOME and reproduces what the Step 0 probe saw of the real tool; the real `defaults` and `cfprefsd` are never run, and
a monkeypatch makes the store use the fake. Key names are the real ones (`tests/fixtures/iterm2/`); values are
invented. The catalog is the one the CLI wires, with the plist adapter.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.adapters.plist import PlistAdapter
from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.commitments import loads_commitments
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import COMMITMENTS_PATH, MANIFEST_PATH, object_path
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.iterm2 import DOMAIN, EXCLUDED_NAMES, SYNCED_NAMES, VIEW
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport, sync
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.document_files import open_document_skips, open_live_files
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
A_SECRET = "invented-bearer-token-of-machine-a"
WORK = "11111111-AAAA-4AAA-8AAA-000000000001"
LOGS = "11111111-AAAA-4AAA-8AAA-000000000002"
WINDOW_FRAME = "NSWindow Frame iTerm Profile 00000000-0000-4000-8000-000000000000 0"


def _profile(guid: str, name: str, **changes: Any) -> dict:
    return {"Guid": guid, "Name": name, "Normal Font": "Menlo-Regular 12", "Columns": 80, "Rows": 25, **changes}


def _domain(**changes: Any) -> dict:
    """What one Mac's domain holds: shared preferences, local state, and a credential-shaped key."""
    values: dict[str, Any] = {
        "AddNewTabAtEndOfTabs": True,
        "NumberOfSpacesPerTab": 4,
        "TabStyle": 1,
        "PromptOnQuit": True,
        "New Bookmarks": [_profile(WORK, "Work")],
        "Default Bookmark Guid": WORK,
        "AICustomHeaders": f"Authorization: Bearer {A_SECRET}",
        WINDOW_FRAME: "10 10 800 600 0 0 1440 900",
        "NoSyncInstallationId": "invented-install-id",
        "AppleWindowTabbingMode": "always",
        "SUFeedURL": "https://example.invalid/appcast.xml",
        "iTerm Version": "3.7.7",
    }
    return {**values, **changes}


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


class FakeRunning:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer

    def running(self, bundle_id: str) -> bool | None:
        return self.answer


class FakeApps:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer

    def installed(self, bundle_id: str) -> bool | None:
        return self.answer


@pytest.fixture(autouse=True)
def _fake_defaults_tool(monkeypatch, fake_defaults) -> None:
    """Every store built in this module runs the fake; the real tool is unreachable from here."""
    monkeypatch.setattr("syncintosh.system.defaults_domains.DEFAULTS_EXECUTABLE", fake_defaults.path)


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


def _initialize(machine: Machine, *, import_from: Path | None = None) -> None:
    machine.home.mkdir(parents=True, exist_ok=True)
    save_config(
        new_machine_config(
            remote=machine.remote, machine_id=machine.machine_id, preset_names=("iterm2",), catalog=CATALOG
        ),
        machine.config_file,
    )
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


def _run(machine: Machine, *, running: Any = None, apps: Any = NO_APP_INFO) -> SyncReport:
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
        adapters={"jsonc": JsoncAdapter(), "plist": PlistAdapter()},
        validators={},
        catalog=CATALOG,
        live=open_live_files(SystemLiveFiles(), str(machine.home), CATALOG.documents()),
        documents=open_document_skips(str(machine.home), CATALOG.documents()),
        ledger_store=JsonLedgerStore(machine.ledger),
        pending_store=DirectoryPendingStore(machine.pending),
        lock=FileLock(machine.lock, timeout_seconds=5),
        deny=SystemDenyList(),
        source=SecureFileIdSource(),
        config=load_config(machine.config_file),
        home=str(machine.home),
        branch=active.branch() or BRANCH,
        apps=apps,
        running=FakeRunning(False) if running is None else running,
    )


def _sealed_view(machine: Machine) -> str:
    """The domain's published object, opened: what an attacker with the key would read."""
    transport = GitCLITransport(machine.repo)
    head = transport.head() or ""
    cipher = EnvelopeCipher(machine.store)
    manifest = loads_manifest(
        cipher.open(transport.read_blob(head, MANIFEST_PATH) or b"", domain="manifest", file_id="")
    )
    (file_id,) = [fid for fid, record in manifest.records.items() if record.path == VIEW]
    sealed = transport.read_blob(head, object_path(file_id)) or b""
    return cipher.open(sealed, domain="object", file_id=file_id).decode("utf-8")


def _pair(tmp_path: Path, make_store, fake, *, b_domain: dict | None = None):
    """A has synced its domain; B has the same preferences plus values of its own."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    fake.seed(a.home, DOMAIN, _domain())
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    if b_domain is not None:
        fake.seed(b.home, DOMAIN, b_domain)
    return a, b


def _b_domain(**changes: Any) -> dict:
    return _domain(
        **{
            "AICustomHeaders": "Authorization: Bearer invented-bearer-token-of-machine-b",
            WINDOW_FRAME: "99 99 500 400 0 0 1440 900",
            "NoSyncInstallationId": "invented-install-id-of-b",
            **changes,
        }
    )


def test_a_preference_edited_on_one_mac_reaches_the_other_and_its_local_values_stay(
    tmp_path, memory_key_store, fake_defaults
) -> None:
    # given — both in sync; A changes a preference
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(TabStyle=2, NumberOfSpacesPerTab=8))

    # when
    _run(a)
    report = _run(b)

    # then — B has A's preferences and every value that is B's own
    values = fake_defaults.values(b.home, DOMAIN)
    assert report.has_conflicts is False
    assert (values["TabStyle"], values["NumberOfSpacesPerTab"]) == (2, 8)
    assert values[WINDOW_FRAME] == "99 99 500 400 0 0 1440 900"
    assert values["NoSyncInstallationId"] == "invented-install-id-of-b"
    assert values["AICustomHeaders"].endswith("machine-b")


def test_apply_goes_through_defaults_import_and_not_through_the_plist(
    tmp_path, memory_key_store, fake_defaults
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(TabStyle=3))
    _run(a)
    plist = b.home / "Library" / "Preferences" / f"{DOMAIN}.plist"
    deny = SystemDenyList()

    # when
    _run(b)

    # then — the tool was asked to import, and syncintosh itself cannot touch the real plist path
    verbs = [call["argv"][0] for call in fake_defaults.calls(b.home)]
    assert ("import" in verbs, deny.is_denied(str(b.home), str(plist.relative_to(b.home)))) == (True, True)


def test_a_key_deleted_on_one_mac_is_deleted_on_the_other(tmp_path, memory_key_store, fake_defaults) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    without = {key: value for key, value in _domain().items() if key != "TabStyle"}
    fake_defaults.seed(a.home, DOMAIN, without)

    # when
    _run(a)
    _run(b)

    # then — `import` alone would have kept it, so the store deleted it
    assert "TabStyle" not in fake_defaults.values(b.home, DOMAIN)
    assert ["delete", DOMAIN, "TabStyle"] in [call["argv"] for call in fake_defaults.calls(b.home)]


def test_the_published_object_holds_no_credential_and_no_key_outside_the_allowlist(
    tmp_path, memory_key_store, fake_defaults
) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store, fake_defaults)

    # when — the object is opened with the key, which is the worst case
    text = _sealed_view(a)

    # then — nothing local, nothing credential-shaped
    leaked = [
        key
        for key in (*EXCLUDED_NAMES, WINDOW_FRAME, "NoSyncInstallationId", "SUFeedURL", "iTerm Version")
        if f"<key>{key}</key>" in text
    ]
    assert (leaked, A_SECRET in text) == ([], False)
    assert "<key>TabStyle</key>" in text
    assert {name for name in SYNCED_NAMES if f"<key>{name}</key>" in text} <= set(SYNCED_NAMES)


@pytest.mark.parametrize(
    "local_change",
    [{WINDOW_FRAME: "1 1 1 1 0 0 1 1"}, {"NoSyncInstallationId": "another"}, {"AppleWindowTabbingMode": "never"}],
)
def test_a_local_only_change_is_no_diff_and_is_never_published(
    tmp_path, memory_key_store, fake_defaults, local_change: dict
) -> None:
    # given
    _, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(b.home, DOMAIN, _b_domain(**local_change))
    head = GitCLITransport(b.repo).head()

    # when
    _run(b)

    # then
    assert GitCLITransport(b.repo).head() == head


@pytest.mark.parametrize("answer", [True, None])
def test_an_open_or_unknowable_iterm2_defers_the_apply_and_collect_proceeds(
    tmp_path, memory_key_store, fake_defaults, answer: bool | None
) -> None:
    # given — A changes a preference; B's iTerm2 is open and B changes one of its own
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(TabStyle=2))
    _run(a)
    fake_defaults.seed(b.home, DOMAIN, _b_domain(NumberOfSpacesPerTab=2))
    imports = [call for call in fake_defaults.calls(b.home) if call["argv"][0] == "import"]

    # when
    report = _run(b, running=FakeRunning(answer))

    # then — the apply waits (no new import), and B's own edit was still published
    assert [d for d in report.deferred if d.path == VIEW and "quit it" in d.reason] != []
    assert fake_defaults.values(b.home, DOMAIN)["TabStyle"] == 1
    assert [call for call in fake_defaults.calls(b.home) if call["argv"][0] == "import"] == imports
    _run(a)
    assert fake_defaults.values(a.home, DOMAIN)["NumberOfSpacesPerTab"] == 2


def test_a_deferred_apply_lands_once_iterm2_has_quit(tmp_path, memory_key_store, fake_defaults) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(TabStyle=2))
    _run(a)
    _run(b, running=FakeRunning(True))

    # when
    report = _run(b, running=FakeRunning(False))

    # then
    assert (VIEW in report.applied, fake_defaults.values(b.home, DOMAIN)["TabStyle"]) == (True, 2)


def test_a_profile_added_on_each_mac_is_kept_on_both(tmp_path, memory_key_store, fake_defaults) -> None:
    # given — each Mac adds a different profile
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(**{"New Bookmarks": [_profile(WORK, "Work"), _profile(LOGS, "Logs")]}))
    other = "11111111-AAAA-4AAA-8AAA-000000000003"
    fake_defaults.seed(b.home, DOMAIN, _b_domain(**{"New Bookmarks": [_profile(WORK, "Work"), _profile(other, "Ops")]}))

    # when
    _run(a)
    report = _run(b)
    _run(a)

    # then — both profiles are on both Macs, merged by `Guid`
    guids = {
        name: sorted(profile["Guid"] for profile in fake_defaults.values(home, DOMAIN)["New Bookmarks"])
        for name, home in (("a", a.home), ("b", b.home))
    }
    assert report.has_conflicts is False
    assert guids["a"] == guids["b"] == sorted([WORK, LOGS, other])


def test_the_same_profile_changed_on_both_sides_is_a_conflict_on_that_profile_only(
    tmp_path, memory_key_store, fake_defaults
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(**{"New Bookmarks": [_profile(WORK, "Work", Columns=100)]}))
    fake_defaults.seed(b.home, DOMAIN, _b_domain(**{"New Bookmarks": [_profile(WORK, "Work", Columns=120)]}))
    _run(a)

    # when
    report = _run(b)

    # then
    assert report.has_conflicts is True


@pytest.mark.parametrize("on", [True, "1", 1])
def test_a_domain_that_loads_its_prefs_from_a_custom_folder_is_skipped_in_collect_and_apply(
    tmp_path, memory_key_store, fake_defaults, on: Any
) -> None:
    # given — A synced; then B turns the custom folder on (or sets something that is not plainly off) and A moves on
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain())
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(TabStyle=2))
    _run(a)
    fake_defaults.seed(b.home, DOMAIN, _b_domain(LoadPrefsFromCustomFolder=on, NumberOfSpacesPerTab=2))
    imports = [call for call in fake_defaults.calls(b.home) if call["argv"][0] == "import"]

    # when
    report = _run(b)
    _run(a)

    # then — nothing collected, nothing applied, and the report says why
    assert [d for d in report.deferred if d.path == VIEW] != []
    assert fake_defaults.values(a.home, DOMAIN)["NumberOfSpacesPerTab"] == 4
    assert [call for call in fake_defaults.calls(b.home) if call["argv"][0] == "import"] == imports
    assert fake_defaults.values(b.home, DOMAIN)["TabStyle"] == 1


def test_a_custom_folder_that_is_off_is_not_a_skip(tmp_path, memory_key_store, fake_defaults) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, fake_defaults, b_domain=_b_domain(LoadPrefsFromCustomFolder=False))
    _run(b)
    fake_defaults.seed(a.home, DOMAIN, _domain(TabStyle=2))
    _run(a)

    # when
    report = _run(b)

    # then
    assert (VIEW in report.applied, fake_defaults.values(b.home, DOMAIN)["TabStyle"]) == (True, 2)


def test_a_mac_without_iterm2_defers_with_the_download_page_and_creates_nothing(
    tmp_path, memory_key_store, fake_defaults
) -> None:
    # given — iTerm2 is not installed on B, so there is no domain
    _, b = _pair(tmp_path, memory_key_store, fake_defaults)

    # when
    report = _run(b, apps=FakeApps(False))

    # then — the shipped catalog's own metadata, and the tool was never asked to import
    (deferral,) = [d for d in report.deferred if d.path == VIEW]
    assert (deferral.download_url, deferral.cask) == ("https://iterm2.com/downloads.html", "iterm2")
    assert [call for call in fake_defaults.calls(b.home) if call["argv"][0] == "import"] == []
    assert (b.home / "Library").exists() is False
