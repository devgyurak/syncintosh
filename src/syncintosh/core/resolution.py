# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator decisions for a conflict, and the identity that ties a decision to one conflict.

A decision is never stored: it is made against the conflict the merge just computed, and it is
only applied while that conflict still exists with the same three sides. The fingerprint is what
makes that check possible — a decision made against one set of inputs cannot land on a different
conflict that later reuses the same path (``docs/design/01-sync-model.md``, resolve).

Pure: standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from syncintosh.core.models import Scope
from syncintosh.core.tree import ElementId, KeyPath

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
    """A stable digest of a value, so a fingerprint never carries the value itself.

    The digest covers every value and its kind, through a structural encoding, not through `repr`: a `repr` may
    hide a value (a setting's text is kept out of it on purpose), and then two different conflicts would share one
    fingerprint, and a pick made for one would settle the other.
    """
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


class _Text:
    """A literal piece of an encoding: a token, not a value, so the encoder never recurses into it."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _encode(value: object) -> str:
    """The structural encoding of `value`, built with an explicit stack so a deeply nested value cannot exhaust
    the recursion limit.

    Each container is written with its kind and its delimiters, mapping keys and set members are written in sorted
    order, and each scalar is written with its type name, so `1`, `1.0`, `"1"` and `True` all differ.
    """
    parts: list[str] = []
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, _Text):
            parts.append(item.text)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            fields = dataclasses.fields(item)
            parts.append(f"<{type(item).__name__}:")
            stack.append(_Text(">"))
            for field_ in reversed(fields):
                stack.append(getattr(item, field_.name))
                stack.append(_Text(f"{field_.name}="))
        elif isinstance(item, Mapping):
            pairs = sorted(item.items(), key=lambda pair: _encode(pair[0]))
            parts.append("{")
            stack.append(_Text("}"))
            for key, child in reversed(pairs):
                stack.append(child)
                stack.append(_Text(f"{_encode(key)}:"))
        elif isinstance(item, (tuple, list)):
            parts.append(f"{type(item).__name__}[")
            stack.append(_Text("]"))
            for child in reversed(item):
                stack.append(child)
                stack.append(_Text(","))
        elif isinstance(item, (set, frozenset)):
            members = sorted(_encode(member) for member in item)
            parts.append(f"{type(item).__name__}{{" + ",".join(members) + "}")
        elif isinstance(item, bytes):
            parts.append(f"bytes({item.hex()})")
        else:
            parts.append(f"{type(item).__name__}({item!r})")
    return "".join(parts)


def fingerprint(file_id: str, key_path: KeyPath | None, base: str, local: str, remote: str) -> str:
    """Identify one conflict by its place and its three side digests.

    `base`, `local` and `remote` are digests (``digest_of`` or a record's ``sha256``), so the
    fingerprint changes whenever any side changes, and never exposes a plaintext value.
    """
    material = repr((file_id, key_path, base, local, remote)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


# A value that someone other than the operator suggests for a content conflict: the whole file, or one
# key's value in the record's own format. The merge parses it and the syntax check validates it, and a
# proposal that fails either one stays a conflict. It holds the user's plaintext, so `repr` never shows it.
@dataclass(frozen=True)
class Proposal:
    text: str = field(repr=False)


# A decision named by a fixed word: the chooser offers these, and only a proposal carries a value.
NamedChoice = Side | RecordChoice | CollisionChoice

# Any decision an operator (or a proposer) can make on one conflict.
Choice = NamedChoice | Proposal

# Decisions keyed by the conflict's fingerprint. A decision applies only to a conflict whose
# fingerprint it was made against.
Picks = Mapping[str, Choice]


@dataclass(frozen=True)
class ConflictView:
    """One conflict as a chooser sees it: where it is, what kind it is, and its fingerprint.

    `key_path` is set only for a key-level conflict; a record or file conflict has `None`.
    `local_present`/`remote_present` say whether that side holds the record at all, which decides
    what "take local" means for a delete/modify. The `*_text` and `diff` fields are the values a
    person reads before choosing. They are plaintext and excluded from `repr`, so a log or a
    traceback never prints them (``AGENTS.md`` §4).
    """

    kind: ConflictKind
    file_id: str
    path: str
    key_path: KeyPath | None
    # A fingerprint identifies a conflict for the pick that settles it; a repr shows it only as nothing (AGENTS.md §4).
    fingerprint: str = field(repr=False)
    local_present: bool = True
    remote_present: bool = True
    # False when a side is not text (a binary file). Such a conflict is never shown to a model, and no text
    # proposal can settle it.
    readable: bool = True
    base_text: str = field(default="", repr=False)
    local_text: str = field(default="", repr=False)
    remote_text: str = field(default="", repr=False)
    diff: str = field(default="", repr=False)
    # Set when the conflict is one binding group of an identified array (`core/keyed_array.py`); `key_path`
    # names the array and this names the group. Its values are the group's bindings, never the whole array.
    element: ElementId | None = field(default=None, repr=False)


# Called with every conflict the cycle currently finds, and the picks this run already made. It
# returns the new picks (never the answered ones again), or `None` to stop without publishing. The
# views carry plaintext (`*_text`, `diff`). A decision that sends any of it to a model or a network
# must mask it first (``AGENTS.md`` §3.5); the interactive chooser only shows it.
Decide = Callable[[tuple[ConflictView, ...], Picks], Picks | None]
