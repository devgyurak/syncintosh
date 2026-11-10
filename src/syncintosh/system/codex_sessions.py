# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex's sessions on this machine: the `SessionStore` that lists them, and the `SessionActivity` that says whether
one is open (issue #56). Layout, resume rules and sources: `presets/codex_sessions.py`.

**Listing.** Codex files a rollout by date, not by project:
`~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<thread>.jsonl`. The project is only inside the file, in the first
`session_meta` record's `payload.cwd`. So the store walks the date tree (strict `\\d{4}/\\d{2}/\\d{2}` folders,
regular files with a canonical rollout name, no `.zst`, never `archived_sessions/`), reads a **bounded** head of
each rollout, keeps only `cwd` from the record — the rest, including the account identifiers and the instructions,
is parsed in memory and dropped — and lists a rollout for the project whose resolved root equals the resolved `cwd`.
Rollouts of one thread (a revert makes a second file) are one session, keyed by the thread id.

**Open sessions.** The writer of a thread holds an exclusive `flock` on `thread-writer-locks/<thread>.lock`; the
file's presence proves nothing, because a crash leaves it. The probe is Codex's own (`writer_lock.rs`): take
`.coordination.lock`, try-lock the thread's lock, release both at once. A lock that cannot be probed leaves the
answer "cannot tell", which every caller treats as open. No lock folder, or no lock file, means no writer. The probe
never creates a file.

**What a person is told** (`note_for`, once per written file): how to resume a session the picker will not list yet
(the picker reads Codex's database only, so a copied rollout is invisible until `codex resume <id>` indexes it,
decision D13); that the rollout it builds on (`history_base`) is not on this Mac; and a different CLI version (D12).
Ids and versions only, never content.

Constructing these objects does no I/O (``AGENTS.md`` §2.6); everything runs inside the port methods.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from syncintosh.core.agent_projects import AgentProject, AgentProjectSpec, agent_project_key
from syncintosh.core.ports import AgentProjectIndex, DenyList, PresetCatalog
from syncintosh.core.sessions import SessionFile, SessionListing, SessionSkipped, SessionUnit

PRESET: Final = "codex-sessions"

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_STAMP: Final = r"rollout-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}"
# One rollout's name; group 1 is the thread id (an ordinary rollout's own id, a reverted one's stable id).
_ROLLOUT: Final = re.compile(rf"{_STAMP}-({_UUID})(?:_({_UUID}))?\.jsonl")
_RELATIVE: Final = re.compile(rf"[0-9]{{4}}/[0-9]{{2}}/[0-9]{{2}}/{_STAMP}-({_UUID})(?:_{_UUID})?\.jsonl")
_THREAD: Final = re.compile(_UUID)
_VERSION: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# ASCII digits only, like the catalog's `{year}` and `{2d}`: the store must not accept a name the catalog refuses.
_YEAR, _TWO = re.compile(r"[0-9]{4}"), re.compile(r"[0-9]{2}")

# A real tree holds tens to thousands of rollouts; a bound keeps a runaway folder from being walked whole.
MAX_ROLLOUTS: Final = 20_000
MAX_SESSION_FILES: Final = 200
# `session_meta` carries the base instructions, so the head can be large; a head past this is not matched, and reported.
MAX_META_BYTES: Final = 1024 * 1024
_META_RECORDS: Final = 8
_PROBE_SECONDS: Final = 2.0
_COORDINATION_ATTEMPTS: Final = 40
_COORDINATION_PAUSE: Final = 0.05


@dataclass(frozen=True)
class _Meta:
    """The three things worth taking from a rollout's `session_meta`; every other value is dropped unread."""

    cwd: str | None
    cli_version: str | None
    history_base: str | None


def read_meta(head: bytes) -> _Meta | None:
    """The `session_meta` among the first records of `head`, or `None` (also when a response comes first)."""
    seen = 0
    for line in head.split(b"\n"):
        if not line.strip():
            continue
        seen += 1
        if seen > _META_RECORDS:
            return None
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if record.get("type") != "session_meta" or not isinstance(payload, dict):
            if record.get("type") in ("response_item", "event_msg"):
                return None
            continue
        cwd, version, base = payload.get("cwd"), payload.get("cli_version"), payload.get("history_base")
        ancestor = base.get("thread_id") if isinstance(base, dict) else None
        return _Meta(
            cwd=cwd if isinstance(cwd, str) and cwd.startswith("/") else None,
            cli_version=version if isinstance(version, str) and _VERSION.fullmatch(version) else None,
            history_base=ancestor if isinstance(ancestor, str) and _THREAD.fullmatch(ancestor) else None,
        )
    return None


def installed_version() -> str | None:
    """The version `codex --version` reports (`codex-cli 0.157.1`), or `None` when it cannot be told."""
    try:
        result = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=_PROBE_SECONDS, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # `ValueError` covers output that is not text (`UnicodeDecodeError`): a version probe never aborts a cycle.
        return None
    found = _VERSION.search(result.stdout) if result.returncode == 0 else None
    return None if found is None else found.group(0)


class CodexSessionStore:
    """Lists the Codex sessions of every project mapped with `codex-sessions`; constructing it does no I/O."""

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
        # `codex --version` runs once per store, however many rollouts a cycle writes.
        self._version_known: list[str | None] = []

    def _enabled(self) -> tuple[tuple[AgentProject, AgentProjectSpec], ...]:
        return tuple(
            (project, layout.spec)
            for project in self._projects
            for layout in project.layouts
            if layout.preset == PRESET
        )

    def _installed(self) -> str | None:
        if not self._version_known:
            self._version_known.append(self._local_version())
        return self._version_known[0]

    # ------------------------------------------------------------------ listing

    def sessions(self, published: frozenset[str] = frozenset()) -> SessionListing:
        """The sessions of every enabled project, plus what could not be listed safely.

        A rollout is a project's when its first record's `cwd` is the project's resolved root, **or** when its record is
        already in the repository under that project's name: a session started on another Mac keeps that Mac's `cwd`
        for good (it is never rewritten), so without the second rule a continuation here would never travel back.
        """
        enabled = self._enabled()
        if not enabled:
            return SessionListing()
        skipped: list[SessionSkipped] = []
        roots: list[tuple[AgentProject, AgentProjectSpec, str]] = []
        for project, spec in enabled:
            found = self._index.read(spec, (project,)).lookup(project.name)
            if found is None or found.directory is None or not found.real_root:
                reason = "the project's folder is not known here" if found is None else found.reason
                skipped.append(SessionSkipped.create(session_id=project.name, reason="unknown", detail=reason))
            else:
                roots.append((project, spec, found.real_root))
        if not roots:
            return SessionListing(skipped=tuple(skipped))
        spec0 = roots[0][1]
        if self._deny.is_denied(self._home, spec0.home) or self._deny.escapes(self._home, f"{spec0.home}/x"):
            return SessionListing(skipped=(*skipped, SessionSkipped.create(session_id="codex", reason="unknown")))
        files, unmatched = self._walk(spec0.home)
        grouped: dict[tuple[str, str], list[SessionFile]] = {}
        names: dict[tuple[str, str], str] = {}
        for relative, thread, size, mtime, meta in files:
            cwd = None if meta is None or meta.cwd is None else os.path.realpath(meta.cwd)
            claimed = [(p, s, r) for p, s, r in roots if s.logical_path(p.name, relative) in published]
            if cwd is None and not claimed:
                unmatched += 1
                continue
            for project, spec, real_root in roots:
                logical = spec.logical_path(project.name, relative)
                if cwd != real_root and logical not in published:
                    continue
                policy = self._catalog.policy_for(logical)
                if policy is None or not policy.append_only:
                    continue
                key = (project.name, thread)
                names[key] = project.name
                grouped.setdefault(key, []).append(
                    SessionFile.create(path=logical, live=f"{spec.home}/{relative}", size=size, mtime=mtime)
                )
        units: list[SessionUnit] = []
        for (project_name, thread), held in sorted(grouped.items()):
            if len(held) > MAX_SESSION_FILES:
                skipped.append(SessionSkipped.create(session_id=thread, reason="cap"))
            else:
                units.append(SessionUnit.create(session_id=thread, project=project_name, files=held))
        if unmatched:
            skipped.append(
                SessionSkipped.create(
                    session_id="codex",
                    reason="unknown",
                    detail=f"{unmatched} rollout(s) could not be matched to a project and were left alone",
                )
            )
        return SessionListing(units=tuple(units), skipped=tuple(skipped))

    def _walk(self, home: str) -> tuple[list[tuple[str, str, int, float, _Meta | None]], int]:
        """Every canonical rollout under the date tree with its parsed `session_meta`, and the unusable entries.

        Each head is parsed as it is read and only the three kept values are held, so a large tree does not hold its
        instructions and account identifiers in memory all at once.
        """
        root = Path(self._home) / home
        found: list[tuple[str, str, int, float, _Meta | None]] = []
        unusable = 0
        for year in self._subdirectories(root, _YEAR):
            for month in self._subdirectories(root / year, _TWO):
                for day in self._subdirectories(root / year / month, _TWO):
                    directory = root / year / month / day
                    try:
                        entries = sorted(os.scandir(directory), key=lambda each: each.name)
                    except OSError:
                        unusable += 1
                        continue
                    for entry in entries:
                        named = _ROLLOUT.fullmatch(entry.name)
                        if named is None:
                            continue
                        if len(found) >= MAX_ROLLOUTS:
                            return found, unusable + 1
                        try:
                            status = entry.stat(follow_symlinks=False)
                            if not stat.S_ISREG(status.st_mode):
                                unusable += 1
                                continue
                            found.append(
                                (
                                    f"{year}/{month}/{day}/{entry.name}",
                                    named.group(1),
                                    status.st_size,
                                    status.st_mtime,
                                    read_meta(_head(Path(entry.path))),
                                )
                            )
                        except OSError:
                            unusable += 1
        return found, unusable

    @staticmethod
    def _subdirectories(parent: Path, pattern: re.Pattern[str]) -> list[str]:
        """The names under `parent` that match `pattern` and are real directories (a link is never followed)."""
        try:
            return sorted(
                entry.name
                for entry in os.scandir(parent)
                if pattern.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
            )
        except OSError:
            return []

    # ------------------------------------------------------------------ paths

    def session_of(self, path: str) -> str | None:
        """The thread id of a record path under a project this machine enabled, else `None`."""
        for project, spec in self._enabled():
            parsed = spec.parse(path)
            if parsed is None or agent_project_key(parsed.name) != agent_project_key(project.name):
                continue
            found = _RELATIVE.fullmatch(parsed.relative)
            if found is not None:
                return found.group(1)
        return None

    def _live(self, path: str) -> str | None:
        """The `$HOME`-relative path a record path stands for here (the date tree under `~/.codex/sessions`)."""
        for _, spec in self._enabled():
            parsed = spec.parse(path)
            if parsed is not None and _RELATIVE.fullmatch(parsed.relative):
                return f"{spec.home}/{parsed.relative}"
        return None

    def retired(self, path: str) -> bool:
        """True when the rollout was archived (`archived_sessions/<file>`) or compressed (`<file>.zst`) here."""
        live = self._live(path)
        if live is None:
            return False
        home = Path(self._home)
        return os.path.lexists(home / f"{live}.zst") or os.path.lexists(
            home / ".codex" / "archived_sessions" / live.rpartition("/")[2]
        )

    # ------------------------------------------------------------------ what a person is told

    def note_for(self, path: str, data: bytes, *, created: bool) -> str | None:
        """What is worth knowing about a rollout just written: how to resume it, what it lacks, a version difference."""
        thread = self.session_of(path)
        if thread is None:
            return None
        meta = read_meta(data[:MAX_META_BYTES])
        parts: list[str] = []
        if created:
            parts.append(f"it stays out of the Codex picker until it is resumed once: codex resume {thread}")
        local = self._installed()
        if meta is not None and meta.cli_version is not None and local is not None and meta.cli_version != local:
            parts.append(f"it was written by Codex {meta.cli_version}; this Mac runs {local}")
        if meta is not None and meta.history_base is not None and not self._rollout_present(meta.history_base):
            parts.append(f"it builds on rollout {meta.history_base}, which is not on this Mac")
        return f"session {thread}: " + "; ".join(parts) if parts else None

    def _rollout_present(self, rollout_id: str) -> bool:
        """True when a rollout with this id (plain, compressed or archived) is on this machine."""
        root = Path(self._home) / ".codex"
        needles = (f"-{rollout_id}.jsonl", f"_{rollout_id}.jsonl")
        names: list[str] = []
        with contextlib.suppress(OSError):
            names.extend(entry.name for entry in os.scandir(root / "archived_sessions"))
        for year in self._subdirectories(root / "sessions", _YEAR):
            for month in self._subdirectories(root / "sessions" / year, _TWO):
                for day in self._subdirectories(root / "sessions" / year / month, _TWO):
                    try:
                        names.extend(entry.name for entry in os.scandir(root / "sessions" / year / month / day))
                    except OSError:
                        continue
        return any(name.removesuffix(".zst").endswith(needles) for name in names)


def _head(path: Path) -> bytes:
    """The first `MAX_META_BYTES` of a regular rollout, or nothing when it cannot be read safely.

    Opened without following a link and without blocking, and checked to be a regular file once open, so a path swapped
    for a link or a FIFO between the listing and this read cannot redirect or hang it.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return b""
        return os.read(descriptor, MAX_META_BYTES)
    except OSError:
        return b""
    finally:
        os.close(descriptor)


class CodexSessionActivity:
    """Whether Codex has a thread open, by the flock its writer holds on `thread-writer-locks/<thread>.lock`."""

    def __init__(self, home: str) -> None:
        self._directory = Path(home) / ".codex" / "thread-writer-locks"

    def is_open(self, session_id: str) -> bool | None:
        """`True` when a writer holds the thread, `False` when none does, `None` when that cannot be told."""
        if _THREAD.fullmatch(session_id) is None:
            return None
        try:
            if not stat.S_ISDIR(self._directory.lstat().st_mode):
                return None
        except FileNotFoundError:
            return False
        except OSError:
            return None
        coordination = self._coordinate()
        if coordination is False:
            return None
        try:
            return self._probe(session_id)
        finally:
            if isinstance(coordination, int):
                os.close(coordination)

    def _coordinate(self) -> int | bool | None:
        """The held coordination lock; `None` when there is no such file, `False` when it cannot be taken in time."""
        try:
            descriptor = os.open(self._directory / ".coordination.lock", os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            return False
        for _ in range(_COORDINATION_ATTEMPTS):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    break
                time.sleep(_COORDINATION_PAUSE)
            else:
                return descriptor
        os.close(descriptor)
        return False

    def _probe(self, thread: str) -> bool | None:
        """Try-lock the thread's own lock file and release it at once; the file is never created."""
        try:
            descriptor = os.open(self._directory / f"{thread}.lock", os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        except OSError:
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return True if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else None
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)


__all__ = [
    "MAX_META_BYTES",
    "MAX_ROLLOUTS",
    "MAX_SESSION_FILES",
    "CodexSessionActivity",
    "CodexSessionStore",
    "installed_version",
    "read_meta",
]
