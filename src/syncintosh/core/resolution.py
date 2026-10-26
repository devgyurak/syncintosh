# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator decisions for a conflict, and the identity that ties a decision to one conflict.

A decision is never stored: it is made against the conflict the merge just computed, and it is
only applied while that conflict still exists with the same three sides. The fingerprint is what
makes that check possible — a decision made against one set of inputs cannot land on a different
conflict that later reuses the same path (``docs/design/01-sync-model.md``, resolve).

Pure: standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from syncintosh.core.models import Scope
from syncintosh.core.tree import KeyPath

# A fingerprint is never stored or sent outside the run that made it. It is an unsalted digest of
# the conflict's values, so writing it to disk or to a network would let anyone with a short value
# in the set test it by guessing. If that ever has to happen, key it with a per-run random key (HMAC).

# Which side's value wins a conflict. Used for key paths and for whole-file content.
Side = Literal["local", "remote"]

# A record-level decision. `local`/`remote` pick a side's record; `drop` untracks the path;
# `keep`/`delete` decide a delete/modify (keep the surviving side's record, or drop it).
RecordChoice = Literal["local", "remote", "drop", "keep", "delete"]

# A path-level decision over a PathCollision: keep the file ID the chosen side holds for the
# path, or drop every claimant. The surviving file ID is the smallest one (`record_merge`).
CollisionChoice = Literal["local", "remote", "drop"]

# A PathCollision's identity: the same folded key the record merge groups by.
CollisionKey = tuple[Scope, str | None, str]

# Which conflict family a view describes; the chooser shows different options for each.
ConflictKind = Literal["key", "file", "add/add", "delete/modify", "modify/modify", "collision"]


def digest_of(value: object) -> str:
    """A stable digest of a plain value, so a fingerprint never carries the value itself.

    ``repr`` is deterministic for the scalar, tuple and dataclass values the merge produces.
    """
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def fingerprint(file_id: str, key_path: KeyPath | None, base: str, local: str, remote: str) -> str:
    """Identify one conflict by its place and its three side digests.

    `base`, `local` and `remote` are digests (``digest_of`` or a record's ``sha256``), so the
    fingerprint changes whenever any side changes, and never exposes a plaintext value.
    """
    material = repr((file_id, key_path, base, local, remote)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class ConflictView:
    """One conflict as a chooser sees it: where it is, what kind it is, and its fingerprint.

    `key_path` is set only for a key-level conflict; a record or file conflict has `None`.
    The view carries no values, so it is safe to log or to hand to a proposer.
    """

    kind: ConflictKind
    file_id: str
    path: str
    key_path: KeyPath | None
    fingerprint: str
