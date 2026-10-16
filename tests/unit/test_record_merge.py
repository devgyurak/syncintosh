# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the record-level manifest merge.

The eight rows of ``docs/design/03-manifest.md`` and the three accepted decisions of the #3
plan: metadata is a field-level three-way merge, a both-sides digest change is routed to the
content merge (never a record conflict), and divergent key generations are refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.errors import GenerationDivergence
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.record_merge import ContentMergeTask, merge_manifests, merge_records

ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID2 = "00112233445566778899aabbccddeeff"
SHA1 = "a" * 64
SHA2 = "b" * 64
SHA3 = "c" * 64
HEX32 = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)


def _rec(file_id: str = ID1, sha: str = SHA1, **overrides: Any) -> ManifestRecord:
    fields: dict[str, Any] = {"path": ".zshrc", "preset": "zsh", "scope": "common", "machine": None, "format": "text"}
    return ManifestRecord.create(file_id=file_id, sha256=sha, **{**fields, **overrides})


BASE = _rec(sha=SHA1)
LOCAL_CONTENT = _rec(sha=SHA2)
REMOTE_CONTENT = _rec(sha=SHA3)


def test_base_empty_takes_the_present_side() -> None:
    # when
    from_local = merge_records(None, {ID1: LOCAL_CONTENT}, {})
    from_remote = merge_records(None, {}, {ID1: REMOTE_CONTENT})

    # then
    assert dict(from_local.merged) == {ID1: LOCAL_CONTENT}
    assert dict(from_remote.merged) == {ID1: REMOTE_CONTENT}
    assert from_local.conflicts == from_remote.conflicts == ()


def test_base_empty_identical_additions_take_one() -> None:
    # when
    result = merge_records(None, {ID1: BASE}, {ID1: BASE})

    # then
    assert dict(result.merged) == {ID1: BASE}
    assert result.conflicts == ()


def test_base_empty_different_additions_conflict() -> None:
    # when
    result = merge_records(None, {ID1: LOCAL_CONTENT}, {ID1: REMOTE_CONTENT})

    # then
    assert [conflict.kind for conflict in result.conflicts] == ["add/add"]
    assert result.merged == {}
    assert result.content_merges == ()


def test_one_side_deleted_with_the_other_unchanged_deletes() -> None:
    # when
    local_deleted = merge_records({ID1: BASE}, {}, {ID1: BASE})
    remote_deleted = merge_records({ID1: BASE}, {ID1: BASE}, {})
    both_deleted = merge_records({ID1: BASE}, {}, {})

    # then
    assert local_deleted.deleted == (ID1,)
    assert remote_deleted.deleted == (ID1,)
    assert both_deleted.deleted == (ID1,)
    assert local_deleted.merged == remote_deleted.merged == both_deleted.merged == {}


def test_delete_against_a_modify_conflicts() -> None:
    # when
    local_deleted = merge_records({ID1: BASE}, {}, {ID1: REMOTE_CONTENT})
    remote_deleted = merge_records({ID1: BASE}, {ID1: LOCAL_CONTENT}, {})

    # then
    assert [conflict.kind for conflict in local_deleted.conflicts] == ["delete/modify"]
    assert [conflict.kind for conflict in remote_deleted.conflicts] == ["delete/modify"]
    assert local_deleted.deleted == remote_deleted.deleted == ()


def test_unchanged_and_one_sided_changes_apply() -> None:
    # when
    unchanged = merge_records({ID1: BASE}, {ID1: BASE}, {ID1: BASE})
    local_only = merge_records({ID1: BASE}, {ID1: LOCAL_CONTENT}, {ID1: BASE})
    remote_only = merge_records({ID1: BASE}, {ID1: BASE}, {ID1: REMOTE_CONTENT})
    identical = merge_records({ID1: BASE}, {ID1: LOCAL_CONTENT}, {ID1: LOCAL_CONTENT})

    # then
    assert dict(unchanged.merged) == {ID1: BASE}
    assert dict(local_only.merged) == {ID1: LOCAL_CONTENT}
    assert dict(remote_only.merged) == {ID1: REMOTE_CONTENT}
    assert dict(identical.merged) == {ID1: LOCAL_CONTENT}


def test_metadata_and_content_changes_from_different_sides_both_apply() -> None:
    # given — local changed only content, remote changed only the path
    local = _rec(sha=SHA2)
    remote = _rec(path=".zshenv")

    # when
    result = merge_records({ID1: BASE}, {ID1: local}, {ID1: remote})

    # then
    assert result.conflicts == ()
    assert result.content_merges == ()
    assert result.merged[ID1].path == ".zshenv"
    assert result.merged[ID1].sha256 == SHA2


def test_both_sides_change_content_is_a_content_task_not_a_conflict() -> None:
    # when
    result = merge_records({ID1: BASE}, {ID1: LOCAL_CONTENT}, {ID1: REMOTE_CONTENT})

    # then
    assert result.conflicts == ()
    assert result.merged == {}
    assert [task.file_id for task in result.content_merges] == [ID1]
    task = result.content_merges[0]
    assert (task.base_sha256, task.local_sha256, task.remote_sha256) == (SHA1, SHA2, SHA3)


def test_the_same_metadata_field_changed_on_both_sides_conflicts() -> None:
    # when
    result = merge_records({ID1: BASE}, {ID1: _rec(path=".a")}, {ID1: _rec(path=".b")})

    # then
    assert [conflict.kind for conflict in result.conflicts] == ["modify/modify"]
    assert result.content_merges == ()


def test_a_content_task_completes_with_the_adapter_digest() -> None:
    # given
    task = ContentMergeTask(
        file_id=ID1,
        base_sha256=SHA1,
        local_sha256=SHA2,
        remote_sha256=SHA3,
        path=".zshrc",
        preset="zsh",
        scope="common",
        machine=None,
        format="text",
    )

    # when
    record = task.complete(SHA1)

    # then
    assert record.file_id == ID1
    assert record.sha256 == SHA1
    assert record.path == ".zshrc"


def test_divergent_generations_are_not_merged() -> None:
    # given
    local = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=2, records={})
    remote = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=3, records={})

    # when & then — rotation is fast-forward only and is never merged
    with pytest.raises(GenerationDivergence):
        merge_manifests(None, local, remote)


def test_merge_manifests_passes_the_records_through() -> None:
    # given — a base makes this a one-sided content change, which applies
    base = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={ID1: BASE})
    local = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={ID1: LOCAL_CONTENT})
    remote = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={ID1: BASE})

    # when
    result = merge_manifests(base, local, remote)

    # then
    assert dict(result.merged) == {ID1: LOCAL_CONTENT}


def test_identical_content_dedups_to_the_smaller_id() -> None:
    # given — two ids for one path with identical content (a random-id collision)
    other = _rec(file_id=ID2, path=".zshrc", sha=SHA1)

    # when
    result = merge_records(None, {ID1: BASE}, {ID2: other})

    # then — ID2 is lexicographically smaller, so it survives and ID1's object is dropped
    assert list(result.merged) == [ID2]
    assert result.deduplicated == (ID1,)
    assert result.collisions == ()


def test_two_spellings_of_one_path_collide() -> None:
    # given — `.Zshrc` and `.zshrc` name one file on APFS, so they must not survive as two
    other = _rec(file_id=ID2, path=".Zshrc", sha=SHA2)

    # when
    result = merge_records(None, {ID1: BASE}, {ID2: other})

    # then
    assert len(result.collisions) == 1
    assert result.collisions[0].file_ids == (ID2, ID1)
    assert result.collisions[0].path in (".zshrc", ".Zshrc")


def test_different_content_is_a_path_collision() -> None:
    # given
    other = _rec(file_id=ID2, path=".zshrc", sha=SHA2)

    # when
    result = merge_records(None, {ID1: BASE}, {ID2: other})

    # then
    assert sorted(result.merged) == sorted([ID1, ID2])
    assert len(result.collisions) == 1
    assert result.collisions[0].file_ids == (ID2, ID1)
    assert result.collisions[0].path == ".zshrc"
    assert result.has_conflicts is True


def test_machine_scope_records_on_different_machines_do_not_collide() -> None:
    # given — the secondary key includes the machine, so two Macs may track one path
    this_mac = _rec(file_id=ID1, path=".docker/config.json", scope="machine", machine="m1", format="jsonc")
    other_mac = _rec(file_id=ID2, path=".docker/config.json", scope="machine", machine="m2", format="jsonc")

    # when
    result = merge_records(None, {ID1: this_mac}, {ID2: other_mac})

    # then
    assert result.collisions == ()
    assert sorted(result.merged) == sorted([ID1, ID2])


def test_a_pending_content_merge_still_collides_by_path() -> None:
    # given — ID1 is a content merge (both sides changed its digest) and ID2 is a new record for
    # the same path, so a task-only sweep would miss the second record for one path
    local = {ID1: _rec(sha=SHA2), ID2: _rec(file_id=ID2, sha=SHA3, path=".zshrc")}
    remote = {ID1: _rec(sha=SHA3)}

    # when
    result = merge_records({ID1: _rec(sha=SHA1)}, local, remote)

    # then
    assert [task.file_id for task in result.content_merges] == [ID1]
    assert len(result.collisions) == 1
    assert result.collisions[0].file_ids == (ID2, ID1)


def test_a_conflict_still_collides_by_path() -> None:
    # given — ID1 conflicts because both sides changed its path, and ID2 is a new record whose
    # path matches one side of that conflict
    base = {ID1: _rec(path=".zshrc")}
    local = {ID1: _rec(path=".zshenv"), ID2: _rec(file_id=ID2, sha=SHA2, path=".zshenv")}
    remote = {ID1: _rec(path=".zprofile")}

    # when
    result = merge_records(base, local, remote)

    # then
    assert [conflict.kind for conflict in result.conflicts] == ["modify/modify"]
    assert len(result.collisions) == 1
    assert result.collisions[0].path == ".zshenv"
    assert result.collisions[0].file_ids == (ID2, ID1)


def test_a_conflict_base_path_is_not_a_collision_candidate() -> None:
    # given — ID1's path changes on both sides, and ID2 newly tracks the old base path; the base
    # record cannot survive a conflict, so it must not manufacture a collision at ".zshrc"
    base = {ID1: _rec(path=".zshrc")}
    local = {ID1: _rec(path=".zshenv"), ID2: _rec(file_id=ID2, sha=SHA2, path=".zshrc")}
    remote = {ID1: _rec(path=".zprofile")}

    # when
    result = merge_records(base, local, remote)

    # then
    assert [conflict.kind for conflict in result.conflicts] == ["modify/modify"]
    assert result.collisions == ()


@st.composite
def _record_maps(draw):
    """A record map keyed by unique ids, with a unique path per id so nothing collides."""
    size = draw(st.integers(min_value=0, max_value=4))
    file_ids = draw(st.lists(HEX32, min_size=size, max_size=size, unique=True))
    return {file_id: _rec(file_id=file_id, sha=file_id * 2, path=f"{file_id}.conf") for file_id in file_ids}


@given(local=_record_maps())
def test_identical_inputs_are_a_noop(local) -> None:
    # when
    result = merge_records(local, local, local)

    # then
    assert result.merged == local
    assert result.conflicts == ()
    assert result.content_merges == ()
    assert result.deleted == ()
    assert result.deduplicated == ()


@given(local=_record_maps())
def test_base_empty_identical_sides_take_the_records(local) -> None:
    # when
    result = merge_records(None, local, local)

    # then
    assert result.merged == local
    assert result.conflicts == ()


@given(base=_record_maps(), changed=_record_maps())
def test_one_sided_change_wins(base, changed) -> None:
    # when
    result = merge_records(base, changed, base)

    # then
    assert result.merged == changed
    assert result.conflicts == ()
    assert result.content_merges == ()


@given(base=_record_maps(), local=_record_maps(), remote=_record_maps())
def test_merge_is_symmetric_in_local_and_remote(base, local, remote) -> None:
    # when
    forward = merge_records(base, local, remote)
    backward = merge_records(base, remote, local)

    # then
    assert sorted(forward.merged) == sorted(backward.merged)
    assert sorted(conflict.file_id for conflict in forward.conflicts) == sorted(
        conflict.file_id for conflict in backward.conflicts
    )
    assert sorted(forward.deleted) == sorted(backward.deleted)
    assert sorted(forward.deduplicated) == sorted(backward.deduplicated)
    assert sorted(collision.file_ids for collision in forward.collisions) == sorted(
        collision.file_ids for collision in backward.collisions
    )
    assert sorted(task.file_id for task in forward.content_merges) == sorted(
        task.file_id for task in backward.content_merges
    )
