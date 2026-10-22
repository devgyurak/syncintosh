# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: execute a write plan against the repo, behind the secret scan.

The scan is the encrypt boundary (``docs/design/01-sync-model.md``): every plaintext — the
manifest and each changed object — is scanned **before** anything is sealed or written, so a
refusal leaves the repo exactly as it was (``AGENTS.md`` §3.4). `services` orchestrates the
`Cipher`, `RepoFiles`, and `SecretScanner` ports; it never imports an implementation
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from syncintosh.core.errors import ContentDigestMismatch
from syncintosh.core.manifest import FileFormat, Manifest
from syncintosh.core.manifest_codec import dumps_manifest
from syncintosh.core.ports import Cipher, RepoFiles, SecretScanner
from syncintosh.core.repo import (
    GITATTRIBUTES_CONTENT,
    GITATTRIBUTES_PATH,
    GITIGNORE_CONTENT,
    GITIGNORE_PATH,
    MANIFEST_PATH,
    OBJECTS_DIR,
    object_file_id,
    object_path,
    plan_writes,
)

# A human label for the manifest in a secret-scan message; objects are labelled by their record
# path instead, which is what the user recognises.
MANIFEST_SUBJECT = "manifest"


# The manifest is canonical JSON, so it is scanned as a structured format: nothing in it is a
# shell reference.
MANIFEST_FORMAT: FileFormat = "jsonc"


def _payloads(
    new: Manifest,
    contents: Mapping[str, bytes],
    objects_to_write: tuple[str, ...],
    manifest_plaintext: bytes,
    write_manifest: bool,
) -> list[tuple[str, bytes, FileFormat]]:
    """Every `(subject, plaintext, format)` that must clear the scan, in a stable order."""
    payloads = [
        (new.records[file_id].path, contents[file_id], new.records[file_id].format) for file_id in objects_to_write
    ]
    if write_manifest:
        payloads.append((MANIFEST_SUBJECT, manifest_plaintext, MANIFEST_FORMAT))
    return payloads


def _verify_digests(new: Manifest, file_ids: tuple[str, ...], contents: Mapping[str, bytes]) -> None:
    """Refuse a plaintext whose digest disagrees with its record.

    The digest is the mix-and-match detector (``docs/design/03-manifest.md``); a mismatch would
    commit an object and a manifest that never agree, and the no-rewrite rule would never revisit
    it. Checked before any seal, so a mismatch writes nothing.
    """
    for file_id in file_ids:
        actual = hashlib.sha256(contents[file_id]).hexdigest()
        expected = new.records[file_id].sha256
        if actual != expected:
            raise ContentDigestMismatch(file_id, expected, actual)


def _remove_orphan_objects(files: RepoFiles, new: Manifest) -> None:
    """Delete every stored file under `objects/` that is not a tracked object slot.

    Two cases: an object whose record never landed (or was already removed), and a leftover that
    is not a slot at all — an atomic-write `.tmp` sibling after a crash. A record without its
    object, or an object without a record, is a corrupt repo (``docs/design/03-manifest.md``).
    The sweep is idempotent, so the next sync repairs either.
    """
    for relpath in files.list_paths(OBJECTS_DIR):
        file_id = object_file_id(relpath)
        if file_id is None or file_id not in new.records:
            files.delete(relpath)


def _ensure_repo_metadata(files: RepoFiles) -> None:
    """Keep the repo's own bookkeeping files current.

    `.gitattributes` marks the stored ciphertext binary so a hand-run `git merge` cannot mangle
    it; `.gitignore` keeps an atomic-write `.tmp` leftover out of a hand-run `git add`.
    """
    for relpath, content in (
        (GITATTRIBUTES_PATH, GITATTRIBUTES_CONTENT),
        (GITIGNORE_PATH, GITIGNORE_CONTENT),
    ):
        desired = content.encode("utf-8")
        if files.read(relpath) != desired:
            files.write(relpath, desired)


def write_repo(
    *,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    head: Manifest | None,
    new: Manifest,
    contents: Mapping[str, bytes],
    force_reseal: bool = False,
) -> None:
    """Turn `head` into `new`, sealing each object and the manifest under the scan.

    `contents` maps a `file_id` to the plaintext of its live file; it must hold an entry for
    every object the plan writes, and its digest must equal the record's. State-changing, so it
    returns `None` and raises on failure (``AGENTS.md`` §2.8): a missing plaintext, a digest
    mismatch, or a scan refusal raises before any seal or write happens.

    `new` must be the **complete** manifest: the orphan sweep deletes every stored object absent
    from it, so a manifest filtered to this machine's records would delete another machine's
    objects.

    `force_reseal` re-encrypts **every** record's object, not only the ones whose digest changed.
    A rotation needs it: the plaintexts are unchanged, so `plan_writes` alone would rewrite only
    the manifest and leave every object bound to the retired generation
    (``docs/design/05-key-generations.md``).
    """
    plan = plan_writes(head, new)
    objects_to_write = tuple(sorted(new.records)) if force_reseal else plan.objects_to_write
    manifest_plaintext = dumps_manifest(new)
    missing = sorted(file_id for file_id in objects_to_write if file_id not in contents)
    if missing:
        raise ValueError(f"no plaintext supplied for objects: {missing}")
    _verify_digests(new, objects_to_write, contents)

    # Scan everything first: a refusal must leave the repo untouched.
    for subject, plaintext, fmt in _payloads(new, contents, objects_to_write, manifest_plaintext, plan.write_manifest):
        scanner.scan(plaintext, subject=subject, format=fmt)

    for file_id in objects_to_write:
        blob = cipher.seal(contents[file_id], domain="object", file_id=file_id, key_id=new.key_generation)
        files.write(object_path(file_id), blob)
    if plan.write_manifest:
        blob = cipher.seal(manifest_plaintext, domain="manifest", file_id="", key_id=new.key_generation)
        files.write(MANIFEST_PATH, blob)
    for file_id in plan.objects_to_delete:
        files.delete(object_path(file_id))
    _remove_orphan_objects(files, new)
    _ensure_repo_metadata(files)
