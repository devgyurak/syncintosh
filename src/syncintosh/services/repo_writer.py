# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: execute a write plan against the repo, behind the secret scan.

The scan is the encrypt boundary (``docs/design/01-sync-model.md``): every plaintext — the
manifest and each changed object — is scanned **before** anything is sealed or written, so a
refusal leaves the repo exactly as it was (``AGENTS.md`` §3.4). `services` orchestrates the
`Cipher`, `RepoFiles`, and `SecretScanner` ports; it never imports an implementation
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

from collections.abc import Mapping

from syncintosh.core.manifest import Manifest
from syncintosh.core.manifest_codec import dumps_manifest
from syncintosh.core.ports import Cipher, RepoFiles, SecretScanner
from syncintosh.core.repo import MANIFEST_PATH, object_path, plan_writes

# A human label for the manifest in a secret-scan message; objects are labelled by their record
# path instead, which is what the user recognises.
MANIFEST_SUBJECT = "manifest"


def _payloads(
    new: Manifest,
    contents: Mapping[str, bytes],
    objects_to_write: tuple[str, ...],
    manifest_plaintext: bytes,
    write_manifest: bool,
) -> list[tuple[str, bytes]]:
    """Every `(subject, plaintext)` that must clear the scan, in a stable order."""
    payloads = [(new.records[file_id].path, contents[file_id]) for file_id in objects_to_write]
    if write_manifest:
        payloads.append((MANIFEST_SUBJECT, manifest_plaintext))
    return payloads


def write_repo(
    *,
    files: RepoFiles,
    cipher: Cipher,
    scanner: SecretScanner,
    head: Manifest | None,
    new: Manifest,
    contents: Mapping[str, bytes],
) -> None:
    """Turn `head` into `new`, sealing each object and the manifest under the scan.

    `contents` maps a `file_id` to the plaintext of its live file; it must hold an entry for
    every object the plan writes. State-changing, so it returns `None` and raises on failure
    (``AGENTS.md`` §2.8): a scan refusal raises before any seal or write happens.
    """
    plan = plan_writes(head, new)
    manifest_plaintext = dumps_manifest(new)
    missing = sorted(file_id for file_id in plan.objects_to_write if file_id not in contents)
    if missing:
        raise ValueError(f"no plaintext supplied for objects: {missing}")

    # Scan everything first: a refusal must leave the repo untouched.
    for subject, plaintext in _payloads(new, contents, plan.objects_to_write, manifest_plaintext, plan.write_manifest):
        scanner.scan(plaintext, subject=subject)

    for file_id in plan.objects_to_write:
        blob = cipher.seal(contents[file_id], domain="object", file_id=file_id, key_id=new.key_generation)
        files.write(object_path(file_id), blob)
    if plan.write_manifest:
        blob = cipher.seal(manifest_plaintext, domain="manifest", file_id="", key_id=new.key_generation)
        files.write(MANIFEST_PATH, blob)
    for file_id in plan.objects_to_delete:
        files.delete(object_path(file_id))
