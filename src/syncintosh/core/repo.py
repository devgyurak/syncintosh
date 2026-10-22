# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo layout and the no-rewrite write plan.

A tracked file's ciphertext lives at ``objects/<file_id>.enc`` and the index at
``manifest.enc``; paths, preset names, and scope never appear in a filename
(``docs/design/03-manifest.md`` §3.2). ``plan_writes`` is pure: an object is written only when
its plaintext digest differs from ``head``'s and the manifest only when its canonical bytes
differ, so an unchanged sync touches nothing (``docs/design/01-sync-model.md``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from syncintosh.core.ids import FILE_ID_PATTERN
from syncintosh.core.manifest import Manifest, ManifestRecord
from syncintosh.core.manifest_codec import dumps_manifest

MANIFEST_PATH = "manifest.enc"
OBJECTS_DIR = "objects"
KEYS_DIR = "keys"
COMMITMENTS_PATH = "keys/commitments.json"
GITATTRIBUTES_PATH = ".gitattributes"
GITIGNORE_PATH = ".gitignore"

# A stored object's slot: `objects/<32 lowercase hex>.enc`. Full match, so a nested or unrelated
# path never parses as an object. The ID width comes from `core.ids`, its single owner.
_OBJECT_PATH_RE = re.compile(rf"{OBJECTS_DIR}/({FILE_ID_PATTERN})\.enc")

# A distributed generation wrap: `keys/<generation>.wrap`. The generation is the id of the key
# being wrapped (the reader's new generation), never the one it is wrapped under — that id lives
# in the envelope header (docs/design/05-key-generations.md).
_WRAP_PATH_RE = re.compile(rf"{KEYS_DIR}/([1-9][0-9]*)\.wrap")

# Defense in depth: mark the stored ciphertext binary so a hand-run `git merge` cannot mangle
# it. Sync never calls `git merge` — it builds the tree with `git commit-tree`
# (docs/design/01-sync-model.md).
GITATTRIBUTES_CONTENT = """\
objects/*.enc         binary -diff -merge
manifest.enc          binary -diff -merge
keys/*.wrap           binary -diff -merge
keys/commitments.json binary -diff -merge
"""

# The atomic write leaves `.<name>.<random>.tmp` siblings inside the repo. They are never a
# tracked slot, and ignoring them keeps a crash-leftover out of a hand-run `git add` — the sweep
# in `services/repo_writer` removes them on the next cycle (docs/design/01-sync-model.md).
GITIGNORE_CONTENT = """\
# Leftover atomic-write temp files and OS junk. Repository hygiene, not configuration.
*.tmp
.*.tmp
.DS_Store
"""

# The paths syncintosh manages inside the repo. A commit stages exactly these, so a stray file
# that lands in the working tree — OS junk the repo's own .gitignore does not name, or a leftover
# from a failed write outside `objects/` — can never be swept into a commit
# (docs/design/01-sync-model.md).
TRACKED_PATHS: tuple[str, ...] = (OBJECTS_DIR, MANIFEST_PATH, KEYS_DIR, GITATTRIBUTES_PATH, GITIGNORE_PATH)


def object_path(file_id: str) -> str:
    """The repo-relative path of `file_id`'s ciphertext."""
    return f"{OBJECTS_DIR}/{file_id}.enc"


def wrap_path(key_id: int) -> str:
    """The repo-relative path of the wrap that distributes generation `key_id`.

    `key_id` is the generation being distributed, so the wrap's filename names the reader's new
    generation and its header names the generation it is wrapped under.
    """
    return f"{KEYS_DIR}/{key_id}.wrap"


def wrap_generation_id(relpath: str) -> int | None:
    """The generation a `keys/<id>.wrap` path distributes, or `None` for anything else."""
    match = _WRAP_PATH_RE.fullmatch(relpath)
    return None if match is None else int(match.group(1))


def object_file_id(relpath: str) -> str | None:
    """The `file_id` a stored-object path names, or `None` for anything else.

    The orphan sweep uses this to recognise a leftover `objects/<id>.enc` without guessing at
    the directory's contents (a record without its object, or an object without a record, is a
    corrupt repo — ``docs/design/03-manifest.md``).
    """
    match = _OBJECT_PATH_RE.fullmatch(relpath)
    return None if match is None else match.group(1)


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
