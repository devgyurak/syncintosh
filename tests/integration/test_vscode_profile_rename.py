# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a VS Code profile renamed in the app keeps its records, on one Mac and across two (issue #67).

A temp bare remote, two temp HOMEs, in-memory key rings, and no network (``AGENTS.md`` §5.1). The shapes — the two Macs'
different profile ids, `storage.json` → `userDataProfiles` — are the ones `test_vscode_profiles.py` already uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _harness import sync_ports
from test_vscode_profiles import (
    INDEX,
    SETTINGS_TEXT,
    USER,
    Machine,
    _pair,
    _profile_dir,
    _register,
    _run,
    _write,
)

from syncintosh.core.config import LocalProfiles, SyncConfig
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.profile_rename import profile_anchor
from syncintosh.core.repo import MANIFEST_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.presets import vscode
from syncintosh.services.diff import diff_repo
from syncintosh.services.sync import ProfileRenamed
from syncintosh.system.config import load_config, save_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.profile_index import SystemProfileIndex
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

A_DIR = "-3f2a1b"
B_DIR = "7c91e0aa"
WORK = vscode.PROFILES.logical_path("Work", "settings.json")
JOB = vscode.PROFILES.logical_path("Job", "settings.json")
JOB_KEYBINDINGS = vscode.PROFILES.logical_path("Job", "keybindings.json")


def _paths(machine: Machine) -> dict[str, str]:
    """`file_id -> path` of every record in the manifest this Mac holds."""
    sealed = GitCLITransport(machine.repo).read_blob(GitCLITransport(machine.repo).head() or "", MANIFEST_PATH) or b""
    manifest = loads_manifest(EnvelopeCipher(machine.store).open(sealed, domain="manifest", file_id=""))
    return {file_id: record.path for file_id, record in manifest.records.items()}


def _profiles_of(machine: Machine) -> list[str]:
    """The profile element (`@Job`) of every record, sorted: one entry per record."""
    return sorted(path.split("/profiles/")[1].split("/")[0] for path in _paths(machine).values())


def _objects(machine: Machine) -> dict[str, bytes]:
    """The sealed bytes of every object, by file name: equal bytes mean nothing was re-encrypted."""
    return {p.name: p.read_bytes() for p in (machine.repo / "objects").iterdir()}


def _ledger(machine: Machine):
    return JsonLedgerStore(machine.ledger).load()


def _rename_in_the_app(machine: Machine, directory: str, name: str) -> None:
    _register(machine, {"name": name, "location": directory})


def _joined(tmp_path: Path, make_store) -> tuple[Machine, Machine]:
    """A and B both have the profile "Work" (different ids) and are in sync."""
    a, b = _pair(tmp_path, make_store)
    _register(b, {"name": "Work", "location": B_DIR})
    _profile_dir(b, B_DIR).mkdir(parents=True)
    _run(b)
    _run(a)
    return a, b


# --------------------------------------------------------------------------- the ledger remembers where a profile was


def test_a_synced_profile_leaves_an_anchor_that_holds_a_digest_and_not_the_name(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)

    # when
    entries = list(_ledger(a).values())

    # then
    assert {(e.profile_dir, e.profile_key) for e in entries} == {(A_DIR, profile_anchor("Work"))}
    assert "Work" not in a.ledger.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- a rename on this Mac


def test_a_rename_here_moves_the_records_and_seals_nothing_again(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    before_paths, before_objects = _paths(a), _objects(a)

    # when
    _rename_in_the_app(a, A_DIR, "Job")
    report = _run(a)

    # then — same ids, every path under the new name, and every object byte-for-byte as it was
    after = _paths(a)
    assert report.renamed == (ProfileRenamed(old="Work", new="Job", records=3),)
    assert (sorted(after), _profiles_of(a)) == (
        sorted(before_paths),
        ["@Job"] * 3,
    )
    assert _objects(a) == before_objects


def test_the_move_settles_so_the_next_sync_has_nothing_to_do(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _rename_in_the_app(a, A_DIR, "Job")
    _run(a)

    # when
    again = _run(a)

    # then
    assert (again.renamed, again.unchanged) == ((), True)
    assert {e.profile_key for e in _ledger(a).values()} == {profile_anchor("Job")}


def test_a_case_only_rename_is_not_a_rename(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    before = _paths(a)

    # when
    _rename_in_the_app(a, A_DIR, "WORK")
    report = _run(a)

    # then
    assert (report.renamed, _paths(a)) == ((), before)


# --------------------------------------------------------------------------- a rename on another Mac


def test_the_other_mac_waits_for_the_rename_and_then_converges(tmp_path, memory_key_store) -> None:
    # given — A renamed the profile and published
    a, b = _joined(tmp_path, memory_key_store)
    _rename_in_the_app(a, A_DIR, "Job")
    _run(a)
    index_before = (b.home / INDEX).read_bytes()

    # when — B still calls its profile Work; the first cycle only learns of the rename, the next one sees it
    _run(b)
    waiting = _run(b)

    # then — nothing moves back, nothing is revived, the profile is left alone, and the report says what to do
    assert [d.reason for d in waiting.deferred if 'renamed to "Job"' in d.reason] != []
    assert set(_profiles_of(b)) == {"@Job"}
    assert (waiting.created, waiting.collected, waiting.failed) == ((), (), False)
    assert (b.home / INDEX).read_bytes() == index_before
    assert (_profile_dir(b, B_DIR) / "settings.json").exists()

    # when — the person renames it in the app, edits a setting, and both sync
    _rename_in_the_app(b, B_DIR, "Job")
    _write(
        b,
        f"{USER}/profiles/{B_DIR}/settings.json",
        SETTINGS_TEXT.replace(b'"editor.tabSize": 4', b'"editor.tabSize": 8'),
    )
    converged = _run(b)
    _run(a)

    # then — in sync: no more waiting, the records are still the same three, and the edit reached A
    assert [d for d in converged.deferred if "renamed to" in d.reason] == []
    assert set(_profiles_of(b)) == {"@Job"}
    assert b'"editor.tabSize": 8' in (_profile_dir(a, A_DIR) / "settings.json").read_bytes()
    assert {e.profile_key for e in _ledger(b).values()} == {profile_anchor("Job")}


def test_a_rename_here_and_an_edit_there_at_once_both_survive(tmp_path, memory_key_store) -> None:
    # given — A renamed the profile (not yet synced) while B edited a setting and published it under the old name
    a, b = _joined(tmp_path, memory_key_store)
    _rename_in_the_app(a, A_DIR, "Job")
    _write(
        b,
        f"{USER}/profiles/{B_DIR}/settings.json",
        SETTINGS_TEXT.replace(b'"editor.tabSize": 4', b'"editor.tabSize": 8'),
    )
    _run(b)

    # when
    report = _run(a)

    # then — the records carry the new name and B's content: one record each, no conflict, nothing duplicated
    assert report.has_conflicts is False
    assert _profiles_of(a) == ["@Job"] * 3
    assert b'"editor.tabSize": 8' in (_profile_dir(a, A_DIR) / "settings.json").read_bytes()


# --------------------------------------------------------------------------- left alone, and reported


def test_an_unreadable_profile_list_moves_nothing(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    before = _paths(a)
    _write(a, INDEX, b"{ not json")

    # when
    report = _run(a)

    # then — a profile this Mac cannot see is not one it renamed
    assert (report.renamed, _paths(a), report.failed) == ((), before, False)


def test_a_new_name_the_config_does_not_allow_is_reported_and_not_moved(tmp_path, memory_key_store) -> None:
    # given — this machine syncs only the profile "Work"
    a, _ = _pair(tmp_path, memory_key_store)
    config = load_config(a.config_file)
    narrowed = SyncConfig.create(
        remote=config.remote,
        machine_id=config.machine_id,
        items=config.items,
        globs=config.globs,
        profiles=(LocalProfiles.create(preset="vscode", spec=vscode.PROFILES, scope="common", names=("Work",)),),
    )
    save_config(narrowed, a.config_file)
    before = _paths(a)

    # when
    _rename_in_the_app(a, A_DIR, "Job")
    report = _run(a)

    # then
    assert (report.renamed, _paths(a), report.failed) == ((), before, False)
    assert [r for r in report.refused if "does not name the new name" in r.reason and not r.blocking] != []


def test_a_new_name_that_already_has_records_is_reported_and_not_moved(tmp_path, memory_key_store) -> None:
    # given — "Job" has its own records, then the profile is removed from the list and "Work" takes its name
    a, _ = _pair(tmp_path, memory_key_store)
    _register(a, {"name": "Work", "location": A_DIR}, {"name": "Job", "location": "11aa22bb"})
    _write(a, f"{USER}/profiles/11aa22bb/settings.json", SETTINGS_TEXT)
    _run(a)
    before = _paths(a)
    _rename_in_the_app(a, A_DIR, "Job")

    # when
    report = _run(a)

    # then — no record of "Work" moved onto a path that already holds one
    assert report.renamed == ()
    assert [p for p in _paths(a).values() if "/@Work/" in p] == [p for p in before.values() if "/@Work/" in p]
    assert [r for r in report.refused if "already exist under the new name" in r.reason and not r.blocking] != []


def test_a_ledger_from_before_the_anchor_is_never_read_as_a_rename(tmp_path, memory_key_store) -> None:
    # given — the ledger as an older build wrote it: no anchor on any entry
    a, _ = _pair(tmp_path, memory_key_store)
    legacy = json.loads(a.ledger.read_text(encoding="utf-8"))
    stripped = {
        "version": legacy["version"],
        "files": {
            file_id: {k: v for k, v in entry.items() if not k.startswith("profile_")}
            for file_id, entry in legacy["files"].items()
        },
    }
    a.ledger.write_text(json.dumps(stripped), encoding="utf-8")

    # when
    _rename_in_the_app(a, A_DIR, "Job")
    report = _run(a)

    # then — treated as before (a known limit): nothing is moved, and the next ledger write adds the anchor
    assert report.renamed == ()
    assert profile_anchor("Job") in {e.profile_key for e in _ledger(a).values()}


def test_a_mac_with_no_anchor_yet_does_not_bring_the_old_name_back(tmp_path, memory_key_store) -> None:
    # given — B was upgraded but has not synced since, so its ledger has no anchor; then A renames and publishes
    a, b = _joined(tmp_path, memory_key_store)
    legacy = json.loads(b.ledger.read_text(encoding="utf-8"))
    b.ledger.write_text(
        json.dumps(
            {
                "version": legacy["version"],
                "files": {
                    file_id: {k: v for k, v in entry.items() if not k.startswith("profile_")}
                    for file_id, entry in legacy["files"].items()
                },
            }
        ),
        encoding="utf-8",
    )
    _rename_in_the_app(a, A_DIR, "Job")
    _run(a)

    # when — B syncs twice with its profile still called Work, and A syncs again
    _run(b)
    waiting = _run(b)
    _run(a)

    # then — the rename was seen from the anchor B took from the records it started the cycle with
    assert [d.reason for d in waiting.deferred if 'renamed to "Job"' in d.reason] != []
    assert (_profiles_of(a), _profiles_of(b)) == (["@Job"] * 3, ["@Job"] * 3)


def test_another_profile_that_merely_has_the_new_name_does_not_replace_the_anchor(tmp_path, memory_key_store) -> None:
    # given — B has an empty profile "Job" that never had records, and A then renames its "Work" to "Job"
    a, b = _joined(tmp_path, memory_key_store)
    _register(b, {"name": "Work", "location": B_DIR}, {"name": "Job", "location": "11aa22bb"})
    _profile_dir(b, "11aa22bb").mkdir(parents=True)
    _rename_in_the_app(a, A_DIR, "Job")
    _run(a)

    # when
    _run(b)
    _run(b)
    _run(a)

    # then — B's Work directory is still the one the records came from, so it is held and no "@Work" comes back
    assert (_profiles_of(a), _profiles_of(b)) == (["@Job"] * 3, ["@Job"] * 3)
    assert {e.profile_key for e in _ledger(b).values()} == {profile_anchor("Work")}


@pytest.mark.parametrize("files", [(), ("settings.json",)], ids=["empty-directory", "directory-with-files"])
def test_a_swap_of_names_moves_nothing_and_says_so_once(tmp_path, memory_key_store, files) -> None:
    # given — the profile is renamed to "Job" and another profile, with or without files, takes the name "Work"
    a, _ = _pair(tmp_path, memory_key_store)
    before = _paths(a)
    _register(a, {"name": "Job", "location": A_DIR}, {"name": "Work", "location": "11aa22bb"})
    _profile_dir(a, "11aa22bb").mkdir(parents=True)
    [_write(a, f"{USER}/profiles/11aa22bb/{name}", SETTINGS_TEXT) for name in files]

    # when
    report = _run(a)
    later = [_run(a) for _ in range(3)]

    # then — the old records keep their name and the renamed directory's files are collected as its own: the content
    # follows the name, not the directory
    after = _paths(a)
    assert report.renamed == ()
    assert {file_id: path for file_id, path in after.items() if file_id in before} == before
    assert [r for r in report.refused if "another profile now has the old name" in r.reason and not r.blocking] != []
    # and the following cycles are quiet: no rename-related row repeats
    assert [(r.reason, r.blocking) for cycle in later for r in cycle.refused] == []
    assert [cycle.renamed for cycle in later] == [()] * 3


def test_two_macs_renaming_to_different_names_end_in_a_conflict_not_a_duplicate(tmp_path, memory_key_store) -> None:
    # given — A published "Job"; B, which has not synced, renamed the same profile to "Gig"
    a, b = _joined(tmp_path, memory_key_store)
    _rename_in_the_app(a, A_DIR, "Job")
    _run(a)
    _rename_in_the_app(b, B_DIR, "Gig")

    # when
    report = _run(b)

    # then — the same records were given two paths: that is a conflict for `resolve`, and nothing is duplicated
    assert (report.has_conflicts, len(_paths(b))) == (True, 3)


# --------------------------------------------------------------------------- diff previews and writes nothing


def test_diff_previews_the_move_without_writing(tmp_path, memory_key_store) -> None:
    # given
    a, _ = _pair(tmp_path, memory_key_store)
    _rename_in_the_app(a, A_DIR, "Job")
    ports = sync_ports(
        a.home, a.store, profiles=SystemProfileIndex(str(a.home), SystemDenyList(), vscode.read_profiles)
    )
    head, ledger = GitCLITransport(a.repo).head(), a.ledger.read_bytes()

    # when
    report = diff_repo(
        transport=ports.transport,
        cipher=ports.cipher,
        live=ports.live,
        config=ports.config,
        home=ports.home,
        deny=ports.deny,
        branch=ports.branch,
        catalog=ports.catalog,
        adapters=ports.adapters,
        validators=ports.validators,
        profiles=ports.profiles,
        ledger=ports.ledger_store.load(),
    )

    # then — the entries are compared under the new name, and neither the repo nor the ledger changed
    assert report.renames == (ProfileRenamed(old="Work", new="Job", records=3),)
    assert sorted(entry.path for entry in report.entries) == sorted(
        [JOB, JOB_KEYBINDINGS, vscode.PROFILES.logical_path("Job", "snippets/js.json")]
    )
    assert (GitCLITransport(a.repo).head(), a.ledger.read_bytes()) == (head, ledger)
