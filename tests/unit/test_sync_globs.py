# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for preset-glob expansion at collect.

A glob is the one place a preset names a directory rather than a file, so the two things worth
pinning are the safety checks each expanded file still gets (deny, ownership, symlink) and the cap
that stops a glob from collecting an unbounded set.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from syncintosh.core.config import LocalGlob, SyncConfig
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.ports import PushOutcome
from syncintosh.core.presets import MAX_GLOB_EXPANSION, Preset, PresetFile
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.sync import NO_APP_INFO, _apply, _Collect, _collect, _expand_globs, _Presets, _settle_ledger
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.live_files import SystemLiveFiles

GLOB = "snippets/*.json"
PRESET = "globbed"


class _RecordingLive(SystemLiveFiles):
    """A `LiveFiles` that remembers every path it was asked to read."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, path: str) -> bytes | None:
        self.reads.append(path)
        return super().read(path)


class _UnusedTransport:
    """A `GitTransport` whose every method fails: the apply path must not read an object."""

    def init(self) -> None:
        raise AssertionError("unused")

    def clone(self, remote: str) -> None:
        raise AssertionError("unused")

    def fetch(self, remote: str, branch: str) -> None:
        raise AssertionError("unused")

    def head(self) -> str | None:
        raise AssertionError("unused")

    def branch(self) -> str | None:
        raise AssertionError("unused")

    def resolve(self, rev: str) -> str | None:
        raise AssertionError("unused")

    def merge_bases(self, left: str, right: str) -> tuple[str, ...]:
        raise AssertionError("unused")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        raise AssertionError("unused")

    def read_blob(self, commit: str, relpath: str) -> bytes | None:
        raise AssertionError("unused")

    def commit(self, parents: tuple[str, ...], message: str, paths: tuple[str, ...]) -> str:
        raise AssertionError("unused")

    def update_ref(self, branch: str, commit: str) -> None:
        raise AssertionError("unused")

    def materialize(self, commit: str) -> None:
        raise AssertionError("unused")

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        raise AssertionError("unused")

    def generations_in_reachable_objects(self, tips: tuple[str, ...]) -> tuple[int, ...]:
        raise AssertionError("unused")

    def remote_reachable(self, remote: str) -> bool:
        raise AssertionError("unused")


class _UnusedCipher:
    """A `Cipher` whose every method fails: a refused record is never decrypted."""

    def seal(self, plaintext: bytes, *, domain, file_id: str, key_id: int) -> bytes:
        raise AssertionError("unused")

    def open(self, blob: bytes, *, domain, file_id: str) -> bytes:
        raise AssertionError("unused")


class _ListingLive:
    """A `LiveFiles` that lists fixed names and reads nothing, for the pure expansion test."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = names

    def read(self, path: str) -> bytes | None:
        return None

    def write(self, path: str, data: bytes) -> None: ...

    def exists(self, path: str) -> bool:
        return False

    def is_symlink(self, path: str) -> bool:
        return False

    def list_dir(self, path: str) -> tuple[str, ...]:
        return self._names

    def remove(self, path: str) -> None: ...


def _config(glob_path: str = GLOB) -> SyncConfig:
    return SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(),
        globs=(LocalGlob.create(path=glob_path, preset=PRESET, scope="common", format="jsonc"),),
    )


def _presets(glob_path: str = GLOB) -> _Presets:
    catalog = BuiltinCatalog.create(
        (Preset.create(name=PRESET, files=(PresetFile.create(path=glob_path, format="jsonc"),)),)
    )
    return _Presets(catalog=catalog, adapters={}, validators={})


def _collect_here(home: Path, glob_path: str = GLOB) -> _Collect:
    return _collect(
        config=_config(glob_path),
        head=None,
        ledger={},
        live=SystemLiveFiles(),
        deny=SystemDenyList(),
        home=str(home),
        adopt=None,
        source=SecureFileIdSource(),
        presets=_presets(glob_path),
    )


def test_expand_globs_turns_a_listing_into_concrete_items() -> None:
    # given — a listing with two matches and one file the pattern does not name
    live = _ListingLive(("a.json", "b.json", "c.txt"))

    # when
    expanded = _expand_globs(_config("d/*.json"), None, live, SystemDenyList(), "/home")

    # then
    assert [item.path for item in expanded.items] == ["d/a.json", "d/b.json"]
    assert expanded.refusals == ()


def test_a_denied_path_matched_by_a_glob_is_never_collected(tmp_path) -> None:
    # given — a glob whose directory is a dangerous path, holding a matching file
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "note.json").write_text("{}", encoding="utf-8")

    # when — the directory is denied before it is even listed
    state = _collect_here(home, ".ssh/*.json")

    # then — the glob is refused and nothing under it was collected
    assert [refusal.path for refusal in state.refused] == [".ssh/*.json"]
    assert state.created == []
    assert state.skipped == []


def test_a_symlink_in_a_glob_directory_pointing_at_a_denied_tree_is_refused(tmp_path) -> None:
    # given — a well-owned directory holding a link that resolves into `~/.ssh`
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("secret", encoding="utf-8")
    (home / "snippets" / "link.json").symlink_to(home / ".ssh" / "id_rsa")

    # when
    state = _collect_here(home)

    # then — judged by its real target, the link is denied before anything is read
    assert state.skipped == ["snippets/link.json"]
    assert state.created == []


def test_a_symlink_in_a_glob_directory_is_reported_not_followed(tmp_path) -> None:
    # given — a link to an ordinary file, so the deny-list does not catch it
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    (home / "target.json").write_text("{}", encoding="utf-8")
    (home / "snippets" / "link.json").symlink_to(home / "target.json")

    # when
    state = _collect_here(home)

    # then — collection copies regular files, never through a link
    assert state.symlinks == ["snippets/link.json"]
    assert state.created == []


def test_a_glob_beyond_the_cap_holds_back_only_the_extra(tmp_path) -> None:
    # given — one more matching file than a glob may publish
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    [
        (home / "snippets" / f"f{index:03d}.json").write_text("{}", encoding="utf-8")
        for index in range(MAX_GLOB_EXPANSION + 1)
    ]

    # when
    state = _collect_here(home)

    # then — the slots that are left are filled, and only the extra is held back, by name
    assert len(state.created) == MAX_GLOB_EXPANSION
    assert [refusal.path for refusal in state.refused] == [f"snippets/f{MAX_GLOB_EXPANSION:03d}.json"]
    assert [refusal.blocking for refusal in state.refused] == [False]
    assert [refusal.reason for refusal in state.refused] == ["the glob is full, so this new file is not published"]


def test_the_cap_counts_already_published_records_too(tmp_path) -> None:
    # given — 300 records already published for the glob, and one new local file
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    (home / "snippets" / "extra.json").write_text("{}", encoding="utf-8")
    records = {
        f"{index:032x}": ManifestRecord.create(
            file_id=f"{index:032x}",
            path=f"snippets/f{index}.json",
            preset=PRESET,
            scope="common",
            format="jsonc",
            sha256="a" * 64,
        )
        for index in range(MAX_GLOB_EXPANSION + 44)
    }
    head = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records=records)

    # when — the bound is the published set plus the local files, not just the listing
    state = _collect(
        config=_config(),
        head=head,
        ledger={},
        live=SystemLiveFiles(),
        deny=SystemDenyList(),
        home=str(home),
        adopt=None,
        source=SecureFileIdSource(),
        presets=_presets(),
    )

    # then
    assert [refusal.path for refusal in state.refused] == ["snippets/extra.json"]
    assert state.created == []


def test_a_full_glob_still_collects_an_already_published_file(tmp_path) -> None:
    # given — a glob over its bound, holding one already-published file (edited) and one new one
    home = tmp_path / "home"
    (home / "snippets").mkdir(parents=True)
    (home / "snippets" / "known.json").write_bytes(b'{"a": 2}')
    (home / "snippets" / "new.json").write_text("{}", encoding="utf-8")
    baseline = hashlib.sha256(b'{"a": 1}').hexdigest()
    records = {
        f"{index:032x}": ManifestRecord.create(
            file_id=f"{index:032x}",
            path=f"snippets/f{index}.json",
            preset=PRESET,
            scope="common",
            format="jsonc",
            sha256="a" * 64,
        )
        for index in range(MAX_GLOB_EXPANSION)
    }
    records["f" * 32] = ManifestRecord.create(
        file_id="f" * 32, path="snippets/known.json", preset=PRESET, scope="common", format="jsonc", sha256=baseline
    )
    head = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records=records)
    ledger = {"f" * 32: LedgerEntry(sha256=baseline, commit="b" * 40, suppressed=False)}

    # when — the guard is soft: the published file is collected, the new one is held back
    state = _collect(
        config=_config(),
        head=head,
        ledger=ledger,
        live=SystemLiveFiles(),
        deny=SystemDenyList(),
        home=str(home),
        adopt=None,
        source=SecureFileIdSource(),
        presets=_presets(),
    )

    # then
    assert [refusal.path for refusal in state.refused] == ["snippets/new.json"]
    assert state.collected == ["snippets/known.json"]
    assert state.created == []


def test_a_symlinked_glob_directory_is_never_listed(tmp_path) -> None:
    # given — `snippets` is a symlink to `~/.docker`, so listing it would show another app's files
    home = tmp_path / "home"
    (home / ".docker").mkdir(parents=True)
    (home / ".docker" / "config.json").write_text('{"auths": {}}', encoding="utf-8")
    (home / "snippets").symlink_to(home / ".docker")

    # when — the glob's directory is checked before it is listed
    state = _collect_here(home, "snippets/*.json")

    # then — refused, and nothing was collected under the VS Code policy (which strips nothing)
    assert [refusal.path for refusal in state.refused] == ["snippets/*.json"]
    assert state.created == []


def test_a_denied_glob_directory_is_never_listed(tmp_path) -> None:
    # given — a glob whose directory is a dangerous path
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "note.json").write_text("{}", encoding="utf-8")

    # when
    state = _collect_here(home, ".ssh/*.json")

    # then
    assert [refusal.path for refusal in state.refused] == [".ssh/*.json"]
    assert state.created == []


def test_a_record_is_not_written_through_a_symlinked_glob_directory(tmp_path) -> None:
    # given — `snippets` is a symlink to `~/.docker`, and a record arrived for a file inside it
    home = tmp_path / "home"
    (home / ".docker").mkdir(parents=True)
    (home / "snippets").symlink_to(home / ".docker")
    record = ManifestRecord.create(
        file_id="0" * 32,
        path="snippets/config.json",
        preset=PRESET,
        scope="common",
        format="jsonc",
        sha256="a" * 64,
    )
    manifest = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={record.file_id: record})

    # when
    applied = _apply(
        transport=_UnusedTransport(),
        cipher=_UnusedCipher(),
        presets=_presets(),
        target_commit="c" * 40,
        target_manifest=manifest,
        seen={},
        views={},
        ledger={},
        live=SystemLiveFiles(),
        deny=SystemDenyList(),
        apps=NO_APP_INFO,
        config=_config(),
        home=str(home),
        adopt=None,
    )

    # then — refused before anything is written, and the target the link points at is untouched
    assert [refusal.reason for refusal in applied.refused] == [
        "the glob's directory is a symbolic link or a dangerous path"
    ]
    assert applied.applied == ()
    assert (home / ".docker" / "config.json").exists() is False


def test_settle_does_not_read_through_a_symlinked_glob_directory(tmp_path) -> None:
    # given — `snippets` is a symlink to `~/.docker`, and a record arrived for a file inside it
    home = tmp_path / "home"
    (home / ".docker").mkdir(parents=True)
    (home / ".docker" / "config.json").write_text('{"auths": {}}', encoding="utf-8")
    (home / "snippets").symlink_to(home / ".docker")
    record = ManifestRecord.create(
        file_id="0" * 32,
        path="snippets/config.json",
        preset=PRESET,
        scope="common",
        format="jsonc",
        sha256="a" * 64,
    )
    manifest = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={record.file_id: record})
    live = _RecordingLive()

    # when
    settled = _settle_ledger(
        target_manifest=manifest,
        target_commit="c" * 40,
        ledger={},
        live=live,
        deny=SystemDenyList(),
        presets=_presets(),
        config=_config(),
        home=str(home),
    )

    # then — nothing settled, and nothing was read through the link
    assert settled == {}
    assert live.reads == []
