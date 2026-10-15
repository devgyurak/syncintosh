# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo layout and the no-rewrite write plan.

A tracked file's ciphertext lives at ``objects/<file_id>.enc`` and the index at
``manifest.enc``; paths, preset names, and scope never appear in a filename
(``docs/design/03-manifest.md`` §3.2). ``plan_writes`` is pure: an object is written only when
its plaintext digest differs from ``head``'s and the manifest only when its canonical bytes
differ, so an unchanged sync touches nothing (``docs/design/01-sync-model.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from syncintosh.core.manifest import Manifest, ManifestRecord
from syncintosh.core.manifest_codec import dumps_manifest

MANIFEST_PATH = "manifest.enc"
OBJECTS_DIR = "objects"
GITATTRIBUTES_PATH = ".gitattributes"

# Defense in depth: mark the stored ciphertext binary so a hand-run `git merge` cannot mangle
# it. Sync never calls `git merge` — it builds the tree with `git commit-tree`
# (docs/design/01-sync-model.md).
GITATTRIBUTES_CONTENT = """\
objects/*.enc         binary -diff -merge
manifest.enc          binary -diff -merge
keys/*.wrap           binary -diff -merge
keys/commitments.json binary -diff -merge
"""


def object_path(file_id: str) -> str:
    """The repo-relative path of `file_id`'s ciphertext."""
    return f"{OBJECTS_DIR}/{file_id}.enc"


@dataclass(frozen=True)
class WritePlan:
    """The minimum set of writes that turns `head` into `new`."""

    objects_to_write: tuple[str, ...]
    objects_to_delete: tuple[str, ...]
    write_manifest: bool


def _head_sha256(head_records: Mapping[str, ManifestRecord], file_id: str) -> str | None:
    record = head_records.get(file_id)
    return None if record is None else record.sha256


def plan_writes(head: Manifest | None, new: Manifest) -> WritePlan:
    """Decide the writes for `new` against `head` (``head = None`` means an empty repo).

    An object is written when its plaintext digest differs from `head`'s — metadata-only
    changes leave the ciphertext alone — and the manifest when its canonical bytes differ.
    A record absent from `new` but present at `head` has its object deleted.
    """
    head_records: Mapping[str, ManifestRecord] = {} if head is None else head.records
    changed = (
        file_id for file_id, record in new.records.items() if _head_sha256(head_records, file_id) != record.sha256
    )
    objects_to_write = tuple(sorted(changed))
    objects_to_delete = tuple(sorted(file_id for file_id in head_records if file_id not in new.records))
    write_manifest = head is None or dumps_manifest(head) != dumps_manifest(new)
    return WritePlan(
        objects_to_write=objects_to_write,
        objects_to_delete=objects_to_delete,
        write_manifest=write_manifest,
    )
