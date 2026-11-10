# SPDX-License-Identifier: AGPL-3.0-or-later
"""A coding agent's **session**: the unit that is sealed, skipped, or refused as a whole (issue #56).

A session is one transcript plus the files the agent spills beside it (sub-agent transcripts, large tool results).
They are collected, refused and applied together: a session with one file left out, or one file that carried a secret,
is not a session another Mac can resume. This module holds the pure decisions about a session and the shape of what a
store reports; reading the disk and asking whether the agent has a session open are ports (`core/ports.py`).

* **idle** — a session is sealed only when every file is older than `IDLE_SECONDS` **and** the agent does not have it
  open. A session whose openness cannot be told counts as open (decision D8, D10).
* **caps** — a file over `MAX_FILE_BYTES` of plaintext, or a project over `MAX_PROJECT_BYTES`, is skipped and reported,
  never chunked (decision D8). 48 MiB keeps the ciphertext under GitHub's 50 MiB warning.
* **reports** — `SessionSkipped` names the session, the reason and a fixed phrase; it never carries a transcript value.

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import validate_path

# Why a session was left out. Serialized nowhere; a `Literal`, not an `Enum` (``AGENTS.md`` §4).
SkipReason = Literal["secret", "cap", "forked", "open", "unknown"]

# A session whose newest file is younger than this may still be written to.
IDLE_SECONDS: Final = 30 * 60

MIB: Final = 1024 * 1024
# Plaintext per file, so the sealed object stays under GitHub's 50 MiB warning, and per project in total.
MAX_FILE_BYTES: Final = 48 * MIB
MAX_PROJECT_BYTES: Final = 256 * MIB

# The fixed sentences a report carries. Never a value from a transcript.
_PHRASES: Final[dict[str, str]] = {
    "secret": "a secret scan hit; the whole session is left out",
    "cap": "over the size limit; the session is left out",
    "forked": "continued on both Macs; the repository's copy is kept and the local one is not collected",
    "open": "the agent may have it open, or that could not be told; it is left alone",
    "unknown": "it could not be read safely; the session is left out",
}


@dataclass(frozen=True)
class SessionFile:
    """One file of a session: its record path, where it really lives, and how big and how old it is.

    `path` is the logical `$HOME`-relative path a record carries; `live` is the real `$HOME`-relative path to read and
    write on this machine. `mtime` is seconds since the epoch.
    """

    path: str
    live: str
    size: int
    mtime: float

    @classmethod
    def create(cls, *, path: str, live: str, size: int, mtime: float) -> SessionFile:
        """Validate and freeze one file; never assign fields afterwards."""
        try:
            validated, real = validate_path(path), validate_path(live)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid session file: {exc}") from exc
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ConfigError(f"{validated}: a session file's size must be a non-negative integer, got {size!r}")
        if not isinstance(mtime, (int, float)) or isinstance(mtime, bool) or mtime < 0:
            raise ConfigError(f"{validated}: a session file's mtime must be a non-negative number, got {mtime!r}")
        return cls(path=validated, live=real, size=size, mtime=float(mtime))


@dataclass(frozen=True)
class SessionUnit:
    """One session of one project: every file that must travel together.

    `project` is the name this machine's config gave the project the session belongs to, so the project cap has
    something to group by. `session_id` is what the agent's own open-session signal is keyed by.
    """

    session_id: str
    project: str
    files: tuple[SessionFile, ...]

    @classmethod
    def create(cls, *, session_id: str, project: str, files: Iterable[SessionFile]) -> SessionUnit:
        """Validate and freeze a session; it must name itself and hold at least one file."""
        held = tuple(files)
        if not isinstance(session_id, str) or session_id == "":
            raise ConfigError(f"a session must name itself, got {session_id!r}")
        if not isinstance(project, str) or project == "":
            raise ConfigError(f"session {session_id!r} must name its project")
        if not held:
            raise ConfigError(f"session {session_id!r} holds no file")
        paths = [file.path for file in held]
        if len(set(paths)) != len(paths):
            raise ConfigError(f"session {session_id!r} names one file twice")
        return cls(session_id=session_id, project=project, files=held)

    @property
    def size(self) -> int:
        """The plaintext bytes of every file of the session."""
        return sum(file.size for file in self.files)

    @property
    def newest(self) -> float:
        """The latest mtime of any file of the session."""
        return max(file.mtime for file in self.files)


@dataclass(frozen=True)
class SessionSkipped:
    """A session left out of this cycle, and why. It never blocks the cycle and never carries a transcript value.

    `path` is the record path the reason concerns (the file a scan hit, the file over the cap), or `""` for the
    whole session. `detail` is a fixed phrase, or the scanner's own location text (a label and a line number).
    """

    session_id: str
    reason: SkipReason
    path: str = ""
    detail: str = ""

    @classmethod
    def create(cls, *, session_id: str, reason: SkipReason, path: str = "", detail: str = "") -> SessionSkipped:
        """Build a report; with no `detail`, the reason's fixed phrase is used."""
        return cls(session_id=session_id, reason=reason, path=path, detail=detail or _PHRASES[reason])


@dataclass(frozen=True)
class SessionListing:
    """What a store found for this cycle: the sessions, and the ones it could not safely list."""

    units: tuple[SessionUnit, ...] = ()
    skipped: tuple[SessionSkipped, ...] = ()


def eligibility(unit: SessionUnit, *, now: float, is_open: bool | None) -> SkipReason | None:
    """Why `unit` may not be sealed now, or `None` when it is idle and within the per-file cap.

    `is_open` is the agent's own signal: `True` open, `False` closed, `None` could not be told — which counts as open,
    because sealing a session that is still being written would publish a prefix and then fork on every later append.
    """
    if is_open is not False:
        return "open"
    if now - unit.newest < IDLE_SECONDS:
        return "open"
    if any(file.size > MAX_FILE_BYTES for file in unit.files):
        return "cap"
    return None


def within_project_cap(
    units: Iterable[SessionUnit], *, published: frozenset[str] = frozenset()
) -> tuple[tuple[SessionUnit, ...], tuple[SessionUnit, ...]]:
    """Split `units` into those that fit their project's cap and those that do not.

    Sessions the repository already holds (`published`, by session id) are kept whatever they weigh, and are counted
    first: the cap is a **soft** guard, like a glob's (`MAX_GLOB_EXPANSION`), so a session that is already a record
    cannot be dropped by a sync that pushes the project over the bound. The rest are taken oldest first (then by id),
    so a project's early history keeps travelling and the newest new sessions are what is held back; the order is the
    same every cycle, so a session is not skipped on one cycle and taken on the next.
    """
    spent: dict[str, int] = {}
    kept: list[SessionUnit] = []
    over: list[SessionUnit] = []
    ordered = sorted(units, key=lambda each: (each.session_id not in published, each.newest, each.session_id))
    for unit in ordered:
        total = spent.get(unit.project, 0)
        if unit.session_id not in published and total + unit.size > MAX_PROJECT_BYTES:
            over.append(unit)
            continue
        spent[unit.project] = total + unit.size
        kept.append(unit)
    return tuple(kept), tuple(over)


__all__ = [
    "IDLE_SECONDS",
    "MAX_FILE_BYTES",
    "MAX_PROJECT_BYTES",
    "SessionFile",
    "SessionListing",
    "SessionSkipped",
    "SessionUnit",
    "SkipReason",
    "eligibility",
    "within_project_cap",
]
