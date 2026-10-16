# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pending state on disk: `pending/state.json` plus the unresolved plaintexts.

While this exists, `sync` refuses to start a new merge (``docs/design/01-sync-model.md``). The
unresolved sides are **plaintext** and deliberately live here, outside the repo, and nowhere
else on disk (``AGENTS.md`` §3.3) — a resolver needs them, and the repo must never hold them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from syncintosh.core.errors import PendingCorrupt
from syncintosh.core.ids import is_file_id
from syncintosh.core.models import PendingConflict, PendingState
from syncintosh.system.live_files import SystemLiveFiles

STATE_NAME = "state.json"
CONTENT_DIR = "content"
_SIDES = ("base", "local", "remote")


def _required_commit(parsed: dict, field: str, state_path: Path) -> str:
    """The commit id a pending state must name; a missing or empty one is corrupt."""
    value = parsed.get(field)
    if not isinstance(value, str) or value == "":
        raise PendingCorrupt(f"the pending state {state_path} does not name a {field}")
    return value


class DirectoryPendingStore:
    """A `PendingStore` over one directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def _state_path(self) -> Path:
        return self._root / STATE_NAME

    def is_pending(self) -> bool:
        """True when a previous sync stopped with unresolved conflicts."""
        return self._state_path.is_file()

    def record(self, state: PendingState) -> None:
        """Write the state and the unresolved plaintexts."""
        live = SystemLiveFiles()
        content = self._root / CONTENT_DIR
        conflicts = []
        for conflict in state.conflicts:
            entry = {"file_id": conflict.file_id, "path": conflict.path, "detail": conflict.detail}
            conflicts.append(entry)
            for side in _SIDES:
                blob = getattr(conflict, side)
                if blob is not None:
                    live.write(str(content / f"{conflict.file_id}.{side}"), blob)
        payload = {
            "local_commit": state.local_commit,
            "remote_commit": state.remote_commit,
            "base_commit": state.base_commit,
            "conflicts": conflicts,
        }
        live.write(str(self._state_path), (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    def clear(self) -> None:
        """Remove the whole pending directory. Raises on a failure to remove it."""
        if self._root.exists():
            shutil.rmtree(self._root)

    def load(self) -> PendingState | None:
        """Read the pending state back, or `None` when there is none."""
        if not self._state_path.is_file():
            return None
        try:
            parsed = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PendingCorrupt(f"the pending state {self._state_path} is unreadable: {exc}") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("conflicts"), list):
            raise PendingCorrupt(f"the pending state {self._state_path} must hold a conflicts list")
        return PendingState.create(
            local_commit=_required_commit(parsed, "local_commit", self._state_path),
            remote_commit=_required_commit(parsed, "remote_commit", self._state_path),
            base_commit=None if parsed.get("base_commit") is None else str(parsed["base_commit"]),
            conflicts=tuple(self._parse_conflict(raw) for raw in parsed["conflicts"]),
        )

    def _parse_conflict(self, raw: object) -> PendingConflict:
        if not isinstance(raw, dict) or not {"file_id", "path", "detail"} <= raw.keys():
            raise PendingCorrupt(f"a pending conflict in {self._state_path} is malformed")
        file_id = str(raw["file_id"])
        if not is_file_id(file_id):
            # The id becomes a path under `content/`; a hand-edited state must not point the
            # reader anywhere else.
            raise PendingCorrupt(f"a pending conflict in {self._state_path} names an invalid file id")
        return PendingConflict(
            file_id=file_id,
            path=str(raw["path"]),
            detail=str(raw["detail"]),
            base=self._side_bytes(file_id, "base"),
            local=self._side_bytes(file_id, "local"),
            remote=self._side_bytes(file_id, "remote"),
        )

    def _side_bytes(self, file_id: str, side: str) -> bytes | None:
        try:
            return (self._root / CONTENT_DIR / f"{file_id}.{side}").read_bytes()
        except FileNotFoundError:
            return None


__all__ = ["CONTENT_DIR", "STATE_NAME", "DirectoryPendingStore"]
