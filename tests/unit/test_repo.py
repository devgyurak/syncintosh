# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the repo layout and the no-rewrite write plan."""

from __future__ import annotations

from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.repo import GITATTRIBUTES_CONTENT, MANIFEST_PATH, object_path, plan_writes

ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID2 = "00112233445566778899aabbccddeeff"
SHA1 = "a" * 64
SHA2 = "b" * 64


def _rec(file_id: str = ID1, sha: str = SHA1, path: str = ".zshrc") -> ManifestRecord:
    return ManifestRecord.create(file_id=file_id, path=path, preset="zsh", scope="common", format="text", sha256=sha)


def _manifest(records: dict[str, ManifestRecord] | None = None, generation: int = 1) -> Manifest:
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=generation, records=records or {})


def test_object_path_is_a_fixed_width_slot() -> None:
    # when
    path = object_path(ID1)

    # then
    assert path == f"objects/{ID1}.enc"


def test_manifest_path_and_gitattributes_content_are_fixed() -> None:
    # when & then
    assert MANIFEST_PATH == "manifest.enc"
    assert GITATTRIBUTES_CONTENT.startswith("objects/*.enc         binary -diff -merge\n")
    assert GITATTRIBUTES_CONTENT.endswith("keys/commitments.json binary -diff -merge\n")


def test_an_empty_repo_writes_every_object_and_the_manifest() -> None:
    # given
    new = _manifest({ID1: _rec()})

    # when
    plan = plan_writes(None, new)

    # then
    assert plan.objects_to_write == (ID1,)
    assert plan.objects_to_delete == ()
    assert plan.write_manifest is True


def test_an_unchanged_repo_is_a_no_op() -> None:
    # given
    manifest = _manifest({ID1: _rec()})

    # when
    plan = plan_writes(manifest, manifest)

    # then
    assert plan.objects_to_write == ()
    assert plan.objects_to_delete == ()
    assert plan.write_manifest is False


def test_a_metadata_only_change_does_not_rewrite_the_object() -> None:
    # given — the same digest, a different preset
    head = _manifest({ID1: _rec()})
    new = _manifest(
        {
            ID1: ManifestRecord.create(
                file_id=ID1, path=".zshrc", preset="zsh-new", scope="common", format="text", sha256=SHA1
            )
        }
    )

    # when
    plan = plan_writes(head, new)

    # then
    assert plan.objects_to_write == ()
    assert plan.write_manifest is True


def test_a_content_change_rewrites_only_that_object() -> None:
    # given
    head = _manifest({ID1: _rec(), ID2: _rec(file_id=ID2, sha=SHA1, path=".zshenv")})
    new = _manifest({ID1: _rec(sha=SHA2), ID2: _rec(file_id=ID2, sha=SHA1, path=".zshenv")})

    # when
    plan = plan_writes(head, new)

    # then
    assert plan.objects_to_write == (ID1,)
    assert plan.write_manifest is True


def test_a_removed_record_deletes_its_object() -> None:
    # given
    head = _manifest({ID1: _rec(), ID2: _rec(file_id=ID2, path=".zshenv")})
    new = _manifest({ID1: _rec()})

    # when
    plan = plan_writes(head, new)

    # then
    assert plan.objects_to_delete == (ID2,)
    assert plan.objects_to_write == ()


def test_a_generation_bump_rewrites_the_manifest() -> None:
    # given — the records are unchanged, but the manifest announces a new generation
    head = _manifest({ID1: _rec()}, generation=1)
    new = _manifest({ID1: _rec()}, generation=2)

    # when
    plan = plan_writes(head, new)

    # then
    assert plan.write_manifest is True
    assert plan.objects_to_write == ()
