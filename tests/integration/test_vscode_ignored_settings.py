# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: VS Code's `settingsSync.ignoredSettings` carried across two Macs (issue #23).

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1). The key
names are VS Code's own: `editor.fontSize`, `editor.tabSize`, `window.zoomLevel` (a preset noise key),
`http.proxyAuthorization` (a preset excluded key), `[python]` and `settingsSync.ignoredSettings`. VS Code
carries an ignored key rather than deleting it, and so does this tool: the list itself syncs, and an ignored
key keeps the value the repo already holds (`presets/vscode.py`).
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
from syncintosh.core.ledger import AdoptChoice
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.presets import BuiltinCatalog, vscode
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.services.setup import new_machine_config
from syncintosh.services.sync import NO_APP_INFO, SyncReport
from syncintosh.system.config import save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.profile_index import SystemProfileIndex
from syncintosh.system.repo_files import FileRepo
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
BRANCH = "main"
USER = "Library/Application Support/Code/User"
SETTINGS = f"{USER}/settings.json"
INDEX = f"{USER}/globalStorage/storage.json"
PROFILE_SETTINGS = f"{USER}/profiles/-3f2a1b/settings.json"
LIST = "settingsSync.ignoredSettings"
ADAPTER = JsoncAdapter()


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
    base = new_machine_config(
        remote=machine.remote, machine_id=machine.machine_id, preset_names=("vscode",), catalog=BuiltinCatalog.default()
    )
    save_config(
        SyncConfig.create(
            remote=base.remote,
            machine_id=base.machine_id,
            items=base.items,
            globs=base.globs,
            profiles=base.profiles,
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


def _run(machine: Machine, *, adopt: AdoptChoice | None = None) -> SyncReport:
    GitCLITransport(machine.repo)
    EnvelopeCipher(machine.store)
    return run_sync(
        machine.home,
        machine.store,
        codec=machine.codec,
        adapters={"jsonc": ADAPTER},
        catalog=BuiltinCatalog.default(),
        apps=NO_APP_INFO,
        profiles=SystemProfileIndex(str(machine.home), SystemDenyList(), vscode.read_profiles),
        adopt=adopt,
    )


def _write(machine: Machine, relpath: str, data: dict[str, Any]) -> None:
    """Write a settings file the way a person lays it out: one member per line."""
    target = machine.home / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _live(machine: Machine, relpath: str = SETTINGS) -> dict[str, Any]:
    return json.loads((machine.home / relpath).read_text(encoding="utf-8"))


def _sealed(machine: Machine) -> list[Any]:
    """Every object the machine's clone holds, decrypted and parsed. Only fixtures are ever in them."""
    cipher = EnvelopeCipher(machine.store)
    files = FileRepo(machine.repo)
    return [
        ADAPTER.parse(cipher.open(files.read(rel) or b"", domain="object", file_id=Path(rel).stem).decode()).to_value()
        for rel in files.list_paths("objects")
    ]


def _remote_head(machine: Machine) -> str:
    return subprocess.run(
        ["git", "--git-dir", machine.remote, "rev-parse", BRANCH], capture_output=True, check=True, text=True
    ).stdout.strip()


BASE = {"editor.tabSize": 4, "editor.fontSize": 13, "window.zoomLevel": 2}


def _pair(tmp_path: Path, make_store, content: dict[str, Any] | None = None) -> tuple[Machine, Machine]:
    """A and B, both initialised and in sync on `content` (the base settings by default)."""
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, BASE if content is None else content)
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote)
    _initialize(b, import_from=a.recovery)
    (b.home / USER).mkdir(parents=True)
    _run(b)
    return a, b


# --------------------------------------------------------------------------- 1. carry across two Macs


def test_a_key_the_other_mac_changed_is_not_taken_but_its_other_changes_are(tmp_path, memory_key_store) -> None:
    # given — in sync; B then changes the font size and the tab size and publishes both
    a, b = _pair(tmp_path, memory_key_store)
    _write(b, SETTINGS, {**BASE, "editor.fontSize": 20, "editor.tabSize": 2})
    _run(b)
    # and A, which has not synced since, starts ignoring the font size
    _write(a, SETTINGS, {**BASE, LIST: ["editor.fontSize"]})

    # when
    report = _run(a)

    # then — no conflict, A keeps its own font size, and gets B's tab size and nothing else of B's
    assert report.has_conflicts is False
    assert _live(a) == {"editor.tabSize": 2, "editor.fontSize": 13, "window.zoomLevel": 2, LIST: ["editor.fontSize"]}


def test_the_value_the_repo_holds_for_an_ignored_key_is_the_other_macs(tmp_path, memory_key_store) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store)
    _write(b, SETTINGS, {**BASE, "editor.fontSize": 20})
    _run(b)
    _write(a, SETTINGS, {**BASE, LIST: ["editor.fontSize"]})

    # when
    _run(a)

    # then — A published the list, and the key kept B's value in the repo instead of being erased or overwritten
    assert [obj for obj in _sealed(a) if obj.get("editor.fontSize") == 20 and LIST in obj] != []
    assert [obj for obj in _sealed(a) if obj.get("editor.fontSize") == 13] == []


# --------------------------------------------------------------------------- 2. nothing to publish


def test_changing_only_an_ignored_key_publishes_nothing(tmp_path, memory_key_store) -> None:
    # given — both Macs in sync with the font size ignored
    a, _ = _pair(tmp_path, memory_key_store, {**BASE, LIST: ["editor.fontSize"]})
    before = _remote_head(a)

    # when
    _write(a, SETTINGS, {**BASE, "editor.fontSize": 18, LIST: ["editor.fontSize"]})
    report = _run(a)

    # then
    assert (report.unchanged, _remote_head(a)) == (True, before)
    assert _live(a)["editor.fontSize"] == 18


# --------------------------------------------------------------------------- 3. profiles obey the default's list


def test_the_default_profiles_list_applies_to_a_profiles_settings_too(tmp_path, memory_key_store) -> None:
    # given — the list lives in the default settings; the profile's own settings hold the key too
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, {**BASE, LIST: ["editor.fontSize"]})
    _write(a, INDEX, {"userDataProfiles": [{"name": "Work", "location": "-3f2a1b"}]})
    _write(a, PROFILE_SETTINGS, {"editor.tabSize": 3, "editor.fontSize": 99})

    # when
    report = _run(a)

    # then — the profile's tab size was sealed and its font size was not
    sealed = _sealed(a)
    assert report.has_conflicts is False
    assert [obj for obj in sealed if obj.get("editor.tabSize") == 3] != []
    assert [obj for obj in sealed if obj.get("editor.fontSize") == 99] == []


# --------------------------------------------------------------------------- 4. a malformed list fails closed


@pytest.mark.parametrize("bad", ["editor.fontSize", {"editor.fontSize": True}, ["editor.fontSize", 3], None])
def test_a_list_that_is_not_an_array_of_strings_refuses_every_file_that_obeys_it(
    tmp_path, memory_key_store, bad
) -> None:
    # given
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, {**BASE, LIST: bad})

    # when
    report = _run(a)

    # then — nothing of the file is sealed, and the report says why in words that do not quote the file
    assert [obj for obj in _sealed(a) if "editor.tabSize" in obj] == []
    assert [row.path for row in report.unprojectable] == [SETTINGS]
    assert "editor.fontSize" not in report.unprojectable[0].reason


def test_a_list_that_is_not_an_array_of_strings_also_refuses_to_apply(tmp_path, memory_key_store) -> None:
    # given — A published good settings; B's own list is broken and B has no settings file yet
    a, _ = _pair(tmp_path, memory_key_store)
    remote = a.remote
    c = _machine(tmp_path, memory_key_store, "machine-c", remote)
    _initialize(c, import_from=a.recovery)
    _write(c, SETTINGS, {LIST: "editor.fontSize"})
    _run(c)

    # when
    report = _run(c)

    # then — the broken file is left exactly as it was
    assert [row.path for row in report.unprojectable] == [SETTINGS]
    assert _live(c) == {LIST: "editor.fontSize"}


# --------------------------------------------------------------------------- 5. excluded stays excluded


def test_listing_an_excluded_or_noise_key_never_seals_it(tmp_path, memory_key_store) -> None:
    # given — the list names a credential and a machine-local key; a good source object exists for the file
    a, _ = _pair(tmp_path, memory_key_store)
    _write(
        a,
        SETTINGS,
        {
            **BASE,
            "http.proxyAuthorization": "Basic Zm9vOmJhcg==",
            LIST: ["http.proxyAuthorization", "window.zoomLevel"],
        },
    )

    # when
    _run(a)

    # then — the user's list does not outrank the preset
    sealed = _sealed(a)
    assert [obj for obj in sealed if "http.proxyAuthorization" in obj or "window.zoomLevel" in obj] == []
    assert _live(a)["http.proxyAuthorization"] == "Basic Zm9vOmJhcg=="


# --------------------------------------------------------------------------- 6. no source object


def test_a_file_with_no_object_yet_is_sealed_without_its_ignored_keys(tmp_path, memory_key_store) -> None:
    # given — a first publish: the repo has no object to take the font size from
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, {**BASE, LIST: ["editor.fontSize", "[python]"], "[python]": {"editor.tabSize": 8}})

    # when
    _run(a)

    # then — the ignored keys were left out, the rest was sealed, and the file here is untouched
    sealed = _sealed(a)
    assert [obj for obj in sealed if "editor.fontSize" in obj or "[python]" in obj] == []
    assert [obj for obj in sealed if obj.get("editor.tabSize") == 4] != []
    assert _live(a)["[python]"] == {"editor.tabSize": 8}


# --------------------------------------------------------------------------- 7. dropping a key from the list


def test_a_key_dropped_from_the_list_syncs_from_the_next_cycle(tmp_path, memory_key_store) -> None:
    # given — both Macs ignore the font size; A has its own value
    a, b = _pair(tmp_path, memory_key_store, {**BASE, LIST: ["editor.fontSize"]})
    _write(a, SETTINGS, {**BASE, "editor.fontSize": 15, LIST: ["editor.fontSize"]})
    _run(a)
    # B stops ignoring it
    _write(b, SETTINGS, {**BASE, LIST: []})
    _run(b)

    # when — A syncs: the list that stops ignoring the key arrives with this cycle, so the key is still carried now
    _run(a)
    during = [obj for obj in _sealed(a) if obj.get("editor.fontSize") == 15]
    # and again: the list A reads at the start of the cycle no longer names it
    _run(a)
    after = [obj for obj in _sealed(a) if obj.get("editor.fontSize") == 15]

    # then
    assert (during, len(after)) == ([], 1)
    _run(b)
    assert _live(b)["editor.fontSize"] == 15


# --------------------------------------------------------------------------- the report


def test_a_cancellation_that_cancels_nothing_is_noted_without_quoting_the_list(tmp_path, memory_key_store) -> None:
    # given — `-window.zoomLevel` names a preset noise key the user never listed
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    _write(a, SETTINGS, {**BASE, LIST: ["editor.fontSize", "-window.zoomLevel"]})

    # when
    report = _run(a)

    # then — the noise key stays local, and the report counts the entry instead of naming it
    assert [note for note in report.notes if "1 entries" in note and "zoomLevel" not in note] != []
    assert [obj for obj in _sealed(a) if "window.zoomLevel" in obj] == []


def test_a_list_with_nothing_to_note_adds_no_note(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store, {**BASE, LIST: ["editor.fontSize"]})

    # when
    report = _run(a)

    # then
    assert report.notes == ()


# --------------------------------------------------------------------------- a list that cannot be read


def test_a_symlinked_default_settings_file_stops_a_profile_from_sealing_its_ignored_keys(
    tmp_path, memory_key_store
) -> None:
    # given — the default settings.json is a symlink (so its list cannot be trusted), and the profile's own
    # settings.json holds a key the list names
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, memory_key_store, "machine-a", remote)
    _initialize(a)
    real = a.home / "dotfiles" / "settings.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({**BASE, LIST: ["editor.fontSize"]}), encoding="utf-8")
    (a.home / SETTINGS).parent.mkdir(parents=True)
    (a.home / SETTINGS).symlink_to(real)
    _write(a, INDEX, {"userDataProfiles": [{"name": "Work", "location": "-3f2a1b"}]})
    _write(a, PROFILE_SETTINGS, {"editor.tabSize": 3, "editor.fontSize": 99})

    # when
    report = _run(a)

    # then — the profile file is refused and reported, and nothing of it was sealed
    work = vscode.PROFILES.logical_path("Work", "settings.json")
    assert [row.path for row in report.unprojectable] == [work]
    assert [obj for obj in _sealed(a) if obj.get("editor.fontSize") == 99 or obj.get("editor.tabSize") == 3] == []


# --------------------------------------------------------------------------- joining with an ignored key


def test_a_new_mac_that_ignores_a_key_adopts_local_without_changing_the_repos_value(tmp_path, memory_key_store) -> None:
    # given — A and B share the file; the new Mac C ignores the font size and has its own value for it
    a, b = _pair(tmp_path, memory_key_store)
    c = _machine(tmp_path, memory_key_store, "machine-c", a.remote)
    _initialize(c, import_from=a.recovery)
    _write(c, SETTINGS, {**BASE, "editor.fontSize": 40, LIST: ["editor.fontSize"]})

    # when
    first = _run(c)
    _run(c, adopt="local")
    _run(b)

    # then — C is asked to choose, then the repo keeps the shared font size, C keeps its own, B is unchanged
    assert first.needs_adopt is True
    assert [obj for obj in _sealed(c) if obj.get("editor.fontSize") == 13 and LIST in obj] != []
    assert [obj for obj in _sealed(c) if obj.get("editor.fontSize") == 40] == []
    assert _live(c)["editor.fontSize"] == 40
    assert _live(b)["editor.fontSize"] == 13


# --------------------------------------------------------------------------- the live file's own spelling


def test_apply_leaves_an_ignored_value_exactly_as_the_live_file_spelled_it(tmp_path, memory_key_store) -> None:
    # given — A spells its ignored font size `1e3`; B publishes a tab size change
    a, b = _pair(tmp_path, memory_key_store)
    _write(b, SETTINGS, {**BASE, "editor.tabSize": 2})
    _run(b)
    text = (
        '{\n  "editor.tabSize": 4,\n  "editor.fontSize": 1e3,\n  "window.zoomLevel": 2,\n'
        f'  "{LIST}": ["editor.fontSize"]\n}}\n'
    )
    (a.home / SETTINGS).write_text(text, encoding="utf-8")

    # when
    _run(a)

    # then — only the tab size moved; `1e3` is not rewritten as `1000.0`
    assert (a.home / SETTINGS).read_text(encoding="utf-8") == text.replace('"editor.tabSize": 4', '"editor.tabSize": 2')
