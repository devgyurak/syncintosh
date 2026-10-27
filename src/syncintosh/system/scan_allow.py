# SPDX-License-Identifier: AGPL-3.0-or-later
"""A per-machine, never-synced allowlist for the secret scan's ambiguous shapes.

The scan is a heuristic in front of the real gate (``AGENTS.md`` §6), and a handful of shapes are
genuinely ambiguous: RFC 3986 allows ``,`` and ``;`` in a URL's userinfo, and the very same bytes
separate entries in a list, so the two cannot be told apart from the text. The scan **fails
closed** there — a refused sync is visible and names its reason, a leaked password is neither —
which leaves a user with one legitimate line and no way out but to edit the file (issue #25).

This is that way out, and it is deliberately narrow:

* The list is **local and never synced**. A synced allowlist would let one machine tell another
  what not to scan, so it lives beside the apply ledger, outside the repo (``AGENTS.md`` §3.3),
  and `config.json` keeps its "non-secret values only" invariant (``docs/design/README.md``).
* An entry names a **(path, pattern label, fingerprint)**, never raw text. The fingerprint is an
  HMAC — keyed with a random per-machine key — of the matched **line**, so the file stores no
  plaintext. A bare digest would be reversible for a *short* value (a `sha256("pa")` can simply be
  looked up); a keyed one cannot be. The keying is what makes a fingerprint safe to put in a
  **refusal message or a log**; it says nothing to someone who can read this 0600 file, who by
  definition has the key.
* The fingerprint is what a refusal prints, so a user can allow the exact span they saw without
  the scan ever putting the value in a message, a log, or this file.

An entry is validated the moment it is built (`ScanAllowEntry`), so the CLI cannot record a shape
the loader would later reject — a corrupt allowlist is not repaired by deleting it, because that
would mint a new key and silently invalidate every fingerprint the user had already recorded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from syncintosh.core.errors import ScanAllowCorrupt
from syncintosh.core.ports import WhenTag

# The allowlist format version. Serialized verbatim, so a plain `int` constant, never an `Enum`.
ALLOW_VERSION: Final = 1

# How much of an HMAC tag of a `when` clause is shown: enough to tell two bindings under one key apart.
WHEN_TAG_HEX_LEN = 12

# 128 bits of HMAC-SHA256 as lowercase hex. The value is copied from a refusal message into
# `scan allow`, so it is kept short; a collision inside one machine's list is negligible and would
# only make one extra, still keyed, span pass on that one machine.
FINGERPRINT_HEX_LEN: Final = 32

# The per-machine HMAC key: 32 random bytes, stored hex in the allowlist file (mode 0600).
KEY_BYTES: Final = 32

# A stored fingerprint is fixed width lowercase hex, so a hand-edited file cannot smuggle in a
# shape the scanner would never produce (and therefore never match).
_FINGERPRINT_RE = re.compile(rf"[0-9a-f]{{{FINGERPRINT_HEX_LEN}}}")

# The pattern labels the allowlist may name. They live **here**, with the safety judgement, rather
# than in the pattern tables: a label is allowlistable when its match contains the credential itself,
# so the fingerprint is specific to that one value and allowing it cannot pass a different one. A
# label whose match is fixed text standing in for any credential — the PEM header — must never be
# allowlistable. `secret_scan` imports these for its tables, so the two cannot drift, and
# `test_every_allowable_label_is_a_real_pattern_label` still checks each names a pattern that exists.
#
# Since a new pattern's shape cannot be judged in advance, a label that is not listed here fails
# **closed**: an unlisted label is refused even if a hand-edited allowlist names it.
URL_CREDENTIALS_LABEL: Final = "URL with credentials"
BASIC_LABEL: Final = "HTTP Basic credential"
BEARER_LABEL: Final = "HTTP Bearer credential"
AUTHORIZATION_LABEL: Final = "authorization header"

ALLOWABLE_LABELS: Final = frozenset({URL_CREDENTIALS_LABEL, BASIC_LABEL, BEARER_LABEL, AUTHORIZATION_LABEL})


@dataclass(frozen=True)
class ScanAllowEntry:
    """One allowed span: the file's subject, the pattern's label, and the keyed fingerprint.

    Validated on construction, so no code path — `scan allow` included — can record a shape that
    `load_scan_allow` would refuse on the next run and thus brick the local list.
    """

    path: str
    label: str
    fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.path:
            raise ScanAllowCorrupt("an allowlist entry must name a non-empty path")
        if self.label not in ALLOWABLE_LABELS:
            # Refused, not ignored: allowing a fixed piece of text (`private key`) would pass every
            # credential it stands in for, so only the ambiguous shapes are allowlistable.
            raise ScanAllowCorrupt(
                f"{self.label!r} is not an allowlistable pattern; only {sorted(ALLOWABLE_LABELS)} may be allowed"
            )
        if _FINGERPRINT_RE.fullmatch(self.fingerprint) is None:
            raise ScanAllowCorrupt(
                f"an allowlist fingerprint must be {FINGERPRINT_HEX_LEN} lowercase hex characters, "
                f"exactly as a refusal prints it"
            )


@dataclass(frozen=True)
class ScanAllow:
    """The local allowlist and the key its fingerprints are keyed with.

    Built by `load_scan_allow` (or `new_scan_allow`); a value is never assembled field by field
    (``AGENTS.md`` §2.7).
    """

    key: bytes
    entries: frozenset[ScanAllowEntry]

    def __repr__(self) -> str:
        # The key is an HMAC secret; a traceback or a debugger must not print it (§4).
        return f"ScanAllow(key=<{len(self.key)} bytes masked>, entries={len(self.entries)})"

    def allows(self, path: str, label: str, fingerprint: str) -> bool:
        """True when this exact `(path, label, fingerprint)` was allowed on this machine.

        Compared field by field rather than by building an entry: `allows` must never raise, and a
        malformed fingerprint from a caller is simply not an allowed one.
        """
        return any(
            entry.path == path and entry.label == label and entry.fingerprint == fingerprint for entry in self.entries
        )

    def with_entry(self, entry: ScanAllowEntry) -> ScanAllow:
        """This allowlist plus `entry`, as a new frozen value; never assigns in place."""
        return ScanAllow(key=self.key, entries=self.entries | {entry})


def fingerprint(key: bytes, matched: bytes) -> str:
    """The keyed, truncated fingerprint of one matched span.

    Keyed — not a bare digest — because a fingerprint of a *short* value is otherwise reversible
    from a word list: a plain `sha256("pa")` can be looked up. With a local random key the stored
    value says nothing without the key, so neither the allowlist file nor a refusal message that
    prints the fingerprint reveals the value (``AGENTS.md`` §4).
    """
    return hmac.new(key, matched, hashlib.sha256).hexdigest()[:FINGERPRINT_HEX_LEN]


def new_scan_allow() -> ScanAllow:
    """A fresh allowlist with a new random key and no entries."""
    return ScanAllow(key=os.urandom(KEY_BYTES), entries=frozenset())


def _render(allow: ScanAllow) -> str:
    entries = sorted(
        ({"path": entry.path, "label": entry.label, "fingerprint": entry.fingerprint} for entry in allow.entries),
        key=lambda entry: (entry["path"], entry["label"], entry["fingerprint"]),
    )
    return json.dumps({"version": ALLOW_VERSION, "key": allow.key.hex(), "entries": entries}, indent=2, sort_keys=True)


def save_scan_allow(allow: ScanAllow, path: Path) -> None:
    """Write the allowlist atomically, at mode 0600.

    0600 because the file holds the HMAC key. The write mirrors `system/live_files.py`: a temp
    sibling plus a rename, so a crash never leaves a half-written allowlist that the next load
    would refuse. The mode is set on the temp file, so the key is never briefly world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (_render(allow) + "\n").encode("utf-8")
    descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        Path(tmp_name).chmod(0o600)
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _parse_entry(raw: object, where: str) -> ScanAllowEntry:
    if not isinstance(raw, dict):
        raise ScanAllowCorrupt(f"{where} must be a JSON object")
    unknown = sorted(set(raw) - {"path", "label", "fingerprint"})
    if unknown:
        raise ScanAllowCorrupt(f"{where} has unknown fields: {unknown}")
    path, label, digest = raw.get("path"), raw.get("label"), raw.get("fingerprint")
    if not isinstance(path, str) or not isinstance(label, str) or not isinstance(digest, str):
        raise ScanAllowCorrupt(f"{where} must name a path, a pattern label, and a fingerprint")
    # `ScanAllowEntry` is the single validator, shared with `scan allow`, so a stored entry and a
    # freshly recorded one are held to exactly the same rule.
    return ScanAllowEntry(path=path, label=label, fingerprint=digest)


def load_scan_allow(path: Path) -> ScanAllow:
    """Read the allowlist, creating it with a fresh key when it does not exist yet.

    The key has to be persisted the first time it is needed — a fingerprint only means something
    if the same key produced it — so a missing file is written here rather than answered with an
    ephemeral key that would change on the next run and leave the fingerprint meaningless. Lines
    like this are why the loader is a factory rather than a constructor (``AGENTS.md`` §2.6).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        allow = new_scan_allow()
        save_scan_allow(allow, path)
        return allow
    except OSError as exc:
        raise ScanAllowCorrupt(f"cannot read the scan allowlist {path}: {exc}") from exc
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScanAllowCorrupt(f"the scan allowlist {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ScanAllowCorrupt(f"the scan allowlist {path} must contain a JSON object")
    if parsed.get("version") != ALLOW_VERSION:
        raise ScanAllowCorrupt(f"the scan allowlist {path} has an unsupported version: {parsed.get('version')!r}")
    key_raw = parsed.get("key")
    if not isinstance(key_raw, str):
        raise ScanAllowCorrupt(f"the scan allowlist {path} must hold a key")
    try:
        key = bytes.fromhex(key_raw)
    except ValueError as exc:
        raise ScanAllowCorrupt(f"the scan allowlist {path} has a malformed key") from exc
    if len(key) != KEY_BYTES:
        raise ScanAllowCorrupt(f"the scan allowlist {path} key must be {KEY_BYTES} bytes")
    entries_raw = parsed.get("entries", [])
    if not isinstance(entries_raw, list):
        raise ScanAllowCorrupt(f"the scan allowlist {path} must hold an `entries` list")
    entries = frozenset(_parse_entry(raw, f"entries[{index}]") for index, raw in enumerate(entries_raw))
    return ScanAllow(key=key, entries=entries)


__all__ = [
    "ALLOWABLE_LABELS",
    "ALLOW_VERSION",
    "AUTHORIZATION_LABEL",
    "BASIC_LABEL",
    "BEARER_LABEL",
    "FINGERPRINT_HEX_LEN",
    "KEY_BYTES",
    "URL_CREDENTIALS_LABEL",
    "ScanAllow",
    "ScanAllowEntry",
    "fingerprint",
    "load_scan_allow",
    "new_scan_allow",
    "save_scan_allow",
]


def keyed_when_tag(path: Path) -> WhenTag | None:
    """A tagger for `when` clauses, keyed with this machine's scan key, or `None` when no key exists yet.

    The key is read but never created here: a caller that only shows a diff must not write the allowlist. Without a key
    there is nothing to tag with, so the caller falls back to the hidden form. The tag is an HMAC under a domain label,
    so it cannot be confused with a scan fingerprint, and without the key it is not a guessable digest of the text.
    """
    key = _read_key(path)
    if key is None:
        return None

    def tag(text: str) -> str:
        return hmac.new(key, b"when\x00" + text.encode("utf-8"), hashlib.sha256).hexdigest()[:WHEN_TAG_HEX_LEN]

    return tag


def _read_key(path: Path) -> bytes | None:
    """The stored per-machine key, or `None` when the allowlist does not exist yet. Never creates the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ScanAllowCorrupt(f"cannot read the scan allowlist {path}: {exc}") from exc
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScanAllowCorrupt(f"the scan allowlist {path} is not valid JSON: {exc}") from exc
    key_raw = parsed.get("key") if isinstance(parsed, dict) else None
    if not isinstance(key_raw, str):
        raise ScanAllowCorrupt(f"the scan allowlist {path} has no key")
    try:
        return bytes.fromhex(key_raw)
    except ValueError as exc:
        raise ScanAllowCorrupt(f"the scan allowlist {path} has a malformed key") from exc
