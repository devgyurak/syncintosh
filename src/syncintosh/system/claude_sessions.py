# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claude Code's sessions on this machine: the `SessionStore` that lists them, and the `SessionActivity` that says
whether one is open (issue #56).

**Listing.** For each project this machine mapped with the `claude-sessions` preset, the store derives the project's
real folder (`AgentProjectIndex`) and lists `<uuid>.jsonl` plus the files under `<uuid>/subagents/` and
`<uuid>/tool-results/` that the catalog's patterns own. It reads directory entries and `lstat` — names, sizes, mtimes
— and never a transcript. A symlink anywhere in a session leaves the session out (collection copies regular files,
``AGENTS.md`` §2.4), and a session or a project with more files than the bounds below is left out, reported, rather
than listed whole.

**Open sessions.** Claude Code writes `~/.claude/sessions/<pid>.json` (`pid`, `sessionId`, `procStart`) when a session
starts and removes it on exit, but a crash leaves it behind. So a session is open only when the process is alive
**and** `procStart` still equals what `ps -o lstart= -p <pid>` says under `LC_ALL=C TZ=UTC` — the CLI's own check
(2.1.308) — which tells a recycled pid from the original. Only those three keys are read, from size-bounded regular
files, in memory. A state that cannot be read leaves the answer "cannot tell", which every caller treats as open. A
machine that has no such folder has no open session, which is what lets a fresh Mac receive its first one.

**Version.** A transcript's records carry the CLI `version`. After a session file is written, `note_for` compares the
first record that has one with `claude --version` and returns a one-line warning when they differ (decision D12).
Either version being unknown gives no warning.

Constructing these objects does no I/O (``AGENTS.md`` §2.6); everything runs inside the port methods.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

from syncintosh.core.agent_projects import AgentProject, AgentProjectSpec, agent_project_key
from syncintosh.core.ports import AgentProjectIndex, DenyList, PresetCatalog
from syncintosh.core.sessions import SessionFile, SessionListing, SessionSkipped, SessionUnit

PRESET: Final = "claude-sessions"

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SESSION_OF: Final = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\.jsonl|/.+)")
_VERSION: Final = re.compile(r"\d+\.\d+\.\d+")
# The directories beside a transcript that hold its files, and nothing else is listed.
_SESSION_DIRECTORIES: Final = ("subagents", "tool-results")
_WORKFLOWS: Final = "workflows"

# A real session holds a handful of files; a bound keeps a runaway folder from being walked whole.
MAX_SESSION_FILES: Final = 2_000
MAX_PROJECT_FILES: Final = 20_000
# `~/.claude/sessions/<pid>.json` is a few hundred bytes.
MAX_ACTIVITY_FILES: Final = 2_000
MAX_ACTIVITY_BYTES: Final = 64 * 1024
# How much of a transcript is searched for its `version`, and how many records at most.
_VERSION_WINDOW: Final = 256 * 1024
_VERSION_RECORDS: Final = 20
_PROBE_SECONDS: Final = 2.0


class ClaudeSessionStore:
    """Lists the Claude Code sessions of every project mapped with `claude-sessions`; constructing it does no I/O."""

    def __init__(
        self,
        home: str,
        projects: tuple[AgentProject, ...],
        index: AgentProjectIndex,
        deny: DenyList,
        catalog: PresetCatalog,
        local_version: Callable[[], str | None] | None = None,
    ) -> None:
        self._home = home
        self._projects = projects
        self._index = index
        self._deny = deny
        self._catalog = catalog
        self._local_version = installed_version if local_version is None else local_version
        # `claude --version` runs once per store, however many transcripts a cycle writes.
        self._version_known: list[str | None] = []

    def _installed(self) -> str | None:
        if not self._version_known:
            self._version_known.append(self._local_version())
        return self._version_known[0]

    def _enabled(self) -> tuple[tuple[AgentProject, AgentProjectSpec], ...]:
        return tuple(
            (project, layout.spec)
            for project in self._projects
            for layout in project.layouts
            if layout.preset == PRESET
        )

    def sessions(self) -> SessionListing:
        """The sessions of every enabled project, plus the projects and sessions that could not be listed safely."""
        units: list[SessionUnit] = []
        skipped: list[SessionSkipped] = []
        for project, spec in self._enabled():
            found = self._index.read(spec, (project,)).lookup(project.name)
            if found is None or found.directory is None:
                reason = "the project's folder is not known here" if found is None else found.reason
                skipped.append(SessionSkipped.create(session_id=project.name, reason="unknown", detail=reason))
                continue
            listed, held_back = self._list_project(project, spec, f"{spec.home}/{found.directory}")
            units.extend(listed)
            skipped.extend(held_back)
        return SessionListing(units=tuple(units), skipped=tuple(skipped))

    def _list_project(
        self, project: AgentProject, spec: AgentProjectSpec, folder: str
    ) -> tuple[list[SessionUnit], list[SessionSkipped]]:
        root = Path(self._home) / folder
        if self._deny.is_denied(self._home, folder) or self._deny.escapes(self._home, f"{folder}/x"):
            return [], [SessionSkipped.create(session_id=project.name, reason="unknown")]
        try:
            names = sorted(
                entry.name for entry in os.scandir(root) if _UUID.fullmatch(entry.name.removesuffix(".jsonl"))
            )
        except (FileNotFoundError, NotADirectoryError):
            return [], []
        except OSError:
            return [], [SessionSkipped.create(session_id=project.name, reason="unknown")]
        units: list[SessionUnit] = []
        skipped: list[SessionSkipped] = []
        total = 0
        for session in sorted({name.removesuffix(".jsonl") for name in names}):
            unit, problem = self._list_session(project, spec, folder, session)
            total += 0 if unit is None else len(unit.files)
            if total > MAX_PROJECT_FILES:
                skipped.append(SessionSkipped.create(session_id=session, reason="cap"))
            elif problem is not None:
                skipped.append(problem)
            elif unit is not None:
                units.append(unit)
        return units, skipped

    def _list_session(
        self, project: AgentProject, spec: AgentProjectSpec, folder: str, session: str
    ) -> tuple[SessionUnit | None, SessionSkipped | None]:
        """One session as a unit, `(None, None)` when it has no transcript, or `(None, report)` when it is unsafe."""
        main = Path(self._home) / folder / f"{session}.jsonl"
        try:
            if not stat.S_ISREG(main.lstat().st_mode):
                return None, SessionSkipped.create(session_id=session, reason="unknown", path=f"{session}.jsonl")
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, SessionSkipped.create(session_id=session, reason="unknown")
        relatives = [f"{session}.jsonl"]
        try:
            if (Path(self._home) / folder / session).is_symlink():
                # A linked session folder would list another place's files under this session's name.
                raise _UnsafeError
            for directory in _SESSION_DIRECTORIES:
                relatives.extend(self._regular_files(Path(self._home) / folder / session, directory))
        except _UnsafeError:
            return None, SessionSkipped.create(session_id=session, reason="unknown")
        if len(relatives) > MAX_SESSION_FILES:
            return None, SessionSkipped.create(session_id=session, reason="cap")
        files: list[SessionFile] = []
        for relative in relatives:
            logical = spec.logical_path(project.name, relative)
            policy = self._catalog.policy_for(logical)
            if policy is None or not policy.append_only:
                # A file the preset does not list is not part of the session this tool carries.
                continue
            try:
                status = (Path(self._home) / folder / relative).lstat()
            except OSError:
                # Claude's own cleanup removed it between the listing and now: the session is not whole, so it waits.
                return None, SessionSkipped.create(session_id=session, reason="unknown")
            files.append(
                SessionFile.create(
                    path=logical, live=f"{folder}/{relative}", size=status.st_size, mtime=status.st_mtime
                )
            )
        return SessionUnit.create(session_id=session, project=project.name, files=files), None

    @staticmethod
    def _regular_files(session_directory: Path, name: str) -> list[str]:
        """`<uuid>/<name>/<file>` for every regular file there; raises `_UnsafeError` on a link or an odd entry."""
        found: list[str] = []
        if (session_directory / name).is_symlink():
            raise _UnsafeError
        try:
            entries = list(os.scandir(session_directory / name))
        except (FileNotFoundError, NotADirectoryError):
            return found
        except OSError as exc:
            raise _UnsafeError from exc
        for entry in sorted(entries, key=lambda each: each.name):
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise _UnsafeError from exc
            if name == "subagents" and entry.name == _WORKFLOWS and stat.S_ISDIR(mode):
                # Claude Code 2.1.308 keeps a workflow's sub-agents one level down: `workflows/<run>/agent-*`.
                found.extend(ClaudeSessionStore._workflow_files(session_directory, name))
                continue
            if not stat.S_ISREG(mode):
                raise _UnsafeError
            found.append(f"{session_directory.name}/{name}/{entry.name}")
        return found

    @staticmethod
    def _workflow_files(session_directory: Path, name: str) -> list[str]:
        """`<uuid>/subagents/workflows/<run>/<file>` for every regular file; a link or an odd entry is unsafe."""
        root = session_directory / name / _WORKFLOWS
        found: list[str] = []
        try:
            runs = sorted(os.scandir(root), key=lambda each: each.name)
        except OSError as exc:
            raise _UnsafeError from exc
        for run in runs:
            if run.is_symlink() or not run.is_dir(follow_symlinks=False):
                raise _UnsafeError
            try:
                entries = sorted(os.scandir(run.path), key=lambda each: each.name)
                modes = [(entry.name, entry.stat(follow_symlinks=False).st_mode) for entry in entries]
            except OSError as exc:
                raise _UnsafeError from exc
            if any(not stat.S_ISREG(mode) for _, mode in modes):
                raise _UnsafeError
            found.extend(f"{session_directory.name}/{name}/{_WORKFLOWS}/{run.name}/{file}" for file, _ in modes)
        return found

    def session_of(self, path: str) -> str | None:
        """The session id of a record path under a project this machine enabled, else `None`."""
        for project, spec in self._enabled():
            parsed = spec.parse(path)
            if parsed is None or agent_project_key(parsed.name) != agent_project_key(project.name):
                continue
            found = _SESSION_OF.fullmatch(parsed.relative)
            if found is not None:
                return found.group(1)
        return None

    def note_for(self, path: str, data: bytes) -> str | None:
        """A one-line warning when the transcript was written by a different Claude Code version, else `None`."""
        session = self.session_of(path)
        if session is None or not path.endswith(f"/{session}.jsonl"):
            return None
        written = transcript_version(data)
        local = self._installed()
        if written is None or local is None or written == local:
            return None
        return f"session {session} was written by Claude Code {written}; this Mac runs {local}"


class _UnsafeError(Exception):
    """A session folder holds something this tool does not copy (a link, a directory, a device)."""


def transcript_version(data: bytes) -> str | None:
    """The `version` of the first record that has one, looking only at the start of the transcript; never raises."""
    for line in data[:_VERSION_WINDOW].splitlines()[:_VERSION_RECORDS]:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        version = record.get("version") if isinstance(record, dict) else None
        if isinstance(version, str) and _VERSION.fullmatch(version):
            return version
    return None


def installed_version() -> str | None:
    """The version `claude --version` reports (`2.1.308 (Claude Code)`), or `None` when it cannot be told."""
    try:
        result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=_PROBE_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = _VERSION.match(result.stdout.strip()) if result.returncode == 0 else None
    return None if found is None else found.group(0)


def process_start(pid: int) -> str | None:
    """The process's start time as Claude Code records it (`ps -o lstart=`, C locale, UTC), or `None` when unknown."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PROBE_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = result.stdout.strip()
    return started or None


def process_alive(pid: int) -> bool:
    """True when a process with `pid` exists (a process we may not signal still exists)."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        # No such process, or a pid no process can have (a file with a huge number must not abort the cycle).
        return False
    except PermissionError:
        return True
    return True


class ClaudeSessionActivity:
    """Whether Claude Code has a session open, from its own `~/.claude/sessions/<pid>.json` files."""

    def __init__(
        self,
        home: str,
        alive: Callable[[int], bool] = process_alive,
        started: Callable[[int], str | None] = process_start,
    ) -> None:
        self._directory = Path(home) / ".claude" / "sessions"
        self._alive = alive
        self._started = started

    def is_open(self, session_id: str) -> bool | None:
        """`True` when a live process owns the session, `False` when none does, `None` when that cannot be told."""
        try:
            names = sorted(entry.name for entry in os.scandir(self._directory) if entry.name.endswith(".json"))
        except FileNotFoundError:
            # Claude Code makes this folder when it first runs, so none means no session has ever been open here.
            return False
        except OSError:
            # Something else is where the folder should be (a file, a denied directory): not "no sessions".
            return None
        if len(names) > MAX_ACTIVITY_FILES:
            return None
        unreadable = False
        for name in names:
            record = self._read(self._directory / name)
            if record is None:
                unreadable = True
            elif record[1] == session_id and self._running(record[0], record[2]):
                return True
        return None if unreadable else False

    def _running(self, pid: int, recorded: str | None) -> bool:
        """True when `pid` is alive and, if it recorded a start time, still started then (not a recycled pid)."""
        if not self._alive(pid):
            return False
        if recorded is None:
            # No identity to compare: a live pid is taken as the session's own (the conservative answer).
            return True
        current = self._started(pid)
        return current is None or current == recorded

    @staticmethod
    def _read(path: Path) -> tuple[int, str, str | None] | None:
        """`(pid, sessionId, procStart)` of one file, or `None` when it cannot be read safely. Values are never kept."""
        try:
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_ACTIVITY_BYTES:
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(document, dict):
            return None
        pid, session, start = document.get("pid"), document.get("sessionId"), document.get("procStart")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(session, str):
            return None
        return pid, session, start if isinstance(start, str) and start.strip() else None


__all__ = [
    "MAX_ACTIVITY_BYTES",
    "MAX_ACTIVITY_FILES",
    "MAX_PROJECT_FILES",
    "MAX_SESSION_FILES",
    "ClaudeSessionActivity",
    "ClaudeSessionStore",
    "installed_version",
    "process_alive",
    "process_start",
    "transcript_version",
]
