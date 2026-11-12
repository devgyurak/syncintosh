# SPDX-License-Identifier: AGPL-3.0-or-later
"""The apply ledger on disk: `applied.json`, content digests only.

It lives outside the repo and never inside git (``AGENTS.md`` §3.3). A write is atomic — a
truncated ledger would read as "no baseline" and turn every file into a first-sight prompt, so
the durable write is shared with `SystemLiveFiles` rather than reimplemented here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from syncintosh.core.errors import LedgerCorrupt
from syncintosh.core.models import LedgerEntry
from syncintosh.system.live_files import SystemLiveFiles

LEDGER_VERSION = 1


def _parse_entry(file_id: str, raw: object) -> LedgerEntry:
    if not isinstance(raw, dict):
        raise LedgerCorrupt(f"ledger entry for {file_id!r} must be a JSON object")
    sha256 = raw.get("sha256")
    commit = raw.get("commit")
    suppressed = raw.get("suppressed")
    if not isinstance(sha256, str) or not isinstance(commit, str) or not isinstance(suppressed, bool):
        raise LedgerCorrupt(f"ledger entry for {file_id!r} is missing sha256/commit/suppressed")
    # Absent in a ledger written before release tracking: that reads as "release unknown", never as an error.
    release = raw.get("release", "")
    if not isinstance(release, str):
        raise LedgerCorrupt(f"ledger entry for {file_id!r} has a non-string release")
    # Same rule for the profile anchor: absent means "no anchor", a wrong type means a damaged ledger.
    profile_dir = raw.get("profile_dir", "")
    profile_key = raw.get("profile_key", "")
    if not isinstance(profile_dir, str) or not isinstance(profile_key, str):
        raise LedgerCorrupt(f"ledger entry for {file_id!r} has a non-string profile anchor")
    # Absent is a ledger from before carry tracking (`None`, which is not the same as an empty list).
    carried = raw.get("carried")
    if carried is not None and (not isinstance(carried, list) or not all(isinstance(item, str) for item in carried)):
        raise LedgerCorrupt(f"ledger entry for {file_id!r} has a malformed carried set")
    return LedgerEntry(
        sha256=sha256,
        commit=commit,
        suppressed=suppressed,
        release=release,
        profile_dir=profile_dir,
        profile_key=profile_key,
        carried=None if carried is None else tuple(carried),
    )


class JsonLedgerStore:
    """A `LedgerStore` over one JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Mapping[str, LedgerEntry]:
        """Return the baselines, or an empty map when no ledger exists yet."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise LedgerCorrupt(f"cannot read the ledger {self._path}: {exc}") from exc
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LedgerCorrupt(f"the ledger {self._path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), dict):
            raise LedgerCorrupt(f"the ledger {self._path} must hold a `files` object")
        return {file_id: _parse_entry(file_id, raw) for file_id, raw in parsed["files"].items()}

    def save(self, entries: Mapping[str, LedgerEntry]) -> None:
        """Replace the ledger atomically."""
        # `release`, the profile anchor and the carried set are written only when set, so a ledger without versioned
        # dirs, profiles or an ignore list is unchanged for older builds.
        files = {
            file_id: {"sha256": entry.sha256, "commit": entry.commit, "suppressed": entry.suppressed}
            | ({"release": entry.release} if entry.release else {})
            | (
                {"profile_dir": entry.profile_dir, "profile_key": entry.profile_key}
                if entry.profile_dir and entry.profile_key
                else {}
            )
            | ({} if entry.carried is None else {"carried": list(entry.carried)})
            for file_id, entry in entries.items()
        }
        payload = json.dumps({"version": LEDGER_VERSION, "files": files}, indent=2, sort_keys=True)
        SystemLiveFiles().write(str(self._path), (payload + "\n").encode("utf-8"))


__all__ = ["LEDGER_VERSION", "JsonLedgerStore"]
