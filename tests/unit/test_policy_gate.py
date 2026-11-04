# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the policy view's pass-through and its helpers (issue #24).

The migration itself is exercised end to end in `tests/integration/test_preset_projection.py`; this pins that
everything the view does *not* change reaches the stored repo untouched.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from syncintosh.core.presets import EMPTY_CATALOG
from syncintosh.services.policy_gate import PolicyView


def _view(inner: Mock) -> PolicyView:
    return PolicyView(inner, cipher=Mock(), catalog=EMPTY_CATALOG, project=lambda data, _path, _fmt: data, scanner=None)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("init", ()),
        ("clone", ("remote",)),
        ("fetch", ("remote", "main")),
        ("head", ()),
        ("branch", ()),
        ("resolve", ("main",)),
        ("merge_bases", ("a", "b")),
        ("is_ancestor", ("a", "b")),
        ("commit", ((), "message", ("objects",))),
        ("update_ref", ("main", "c1")),
        ("materialize", ("c1",)),
        ("push", ("remote", "main", "c1")),
        ("generations_in_reachable_objects", (("c1",),)),
        ("remote_reachable", ("remote",)),
    ],
)
def test_everything_but_a_read_goes_to_the_stored_repo(method, args) -> None:
    # given
    inner = Mock()
    view = _view(inner)

    # when
    getattr(view, method)(*args)

    # then
    getattr(inner, method).assert_called_once_with(*args)


def test_a_path_that_is_neither_the_manifest_nor_an_object_is_read_as_stored() -> None:
    # given
    inner = Mock()
    inner.read_blob.return_value = b"commitments"

    # when
    blob = _view(inner).read_blob("c1", "keys/commitments.json")

    # then
    assert blob == b"commitments"


def test_a_commit_without_a_manifest_has_nothing_to_migrate() -> None:
    # given
    inner = Mock()
    inner.read_blob.return_value = None
    view = _view(inner)

    # when
    blob = view.read_blob("c1", "manifest.enc")

    # then
    assert (blob, dict(view.migrated_contents("c1")), view.notes(), view.mismatches) == (None, {}, (), ())


def test_a_view_exposes_the_stored_repo_as_raw() -> None:
    # given
    inner = Mock()

    # when
    raw = _view(inner).raw

    # then
    assert raw is inner


def test_an_owned_record_with_no_policy_or_no_object_is_not_stamped_as_migrated() -> None:
    # given — the preset owns the path but holds no policy for it, so nothing can be projected
    from syncintosh.core.errors import ManifestCorrupt
    from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
    from syncintosh.core.manifest_codec import dumps_manifest
    from syncintosh.core.policy import PolicyStamp
    from syncintosh.core.presets import Preset, PresetFile

    preset = Preset.create(name="x", files=(PresetFile.create(path="x.conf", format="text"),))
    record = ManifestRecord.create(
        file_id="0f3c4b5a69788796a5b4c3d2e1f0abcd",
        path="x.conf",
        preset="x",
        scope="common",
        format="text",
        sha256="a" * 64,
    )
    manifest = Manifest.create(
        schema=MANIFEST_SCHEMA,
        key_generation=1,
        records={record.file_id: record},
        policy={"x": PolicyStamp.create(rev=1, digest="b" * 64)},
    )

    class _Catalog:
        def preset_for(self, path):
            return preset

        def policy_for(self, path):
            return None

        def policy_history(self, name):
            return ("b" * 64, "c" * 64)

        def get(self, name):
            return preset

        def knows(self, preset, path):
            return True

        def ignore_list_files(self):
            return ()

    cipher = Mock()
    cipher.open.return_value = dumps_manifest(manifest)
    inner = Mock()
    inner.read_blob.return_value = b"\x01\x00\x00\x00\x01rest"
    view = PolicyView(inner, cipher=cipher, catalog=_Catalog(), project=lambda d, _p, _f: d, scanner=None)

    # when & then
    with pytest.raises(ManifestCorrupt):
        view.read_blob("c1", "manifest.enc")
