# SPDX-License-Identifier: AGPL-3.0-or-later
"""An `AgentProjectIndex` that says where this machine keeps each mapped project's agent data (issue #56).

Claude Code names a project's folder after the project's **real** absolute path, so the folder is derived here, from
the root this machine's own config maps (`realpath` resolves a symlinked `$HOME`). Three things make a project
unusable, and each is reported rather than guessed past:

* **an environment override** — `CLAUDE_CONFIG_DIR` moves the agent's root and `CLAUDE_CODE_PROJECT_DIR_NAME` renames
  the folder, so the derived name would be wrong. The whole layout is refused for the cycle (decision D3);
* **an unsafe root** — missing, not a directory, a symlink, a dangerous path, or one that leaves `$HOME`;
* **a relocated memory directory** — `autoMemoryDirectory` in the user's or the project's Claude settings. Only that
  one key is read, in memory, from a size-bounded regular file; its value is neither logged nor returned, and a file
  that cannot be read is treated as "relocated" (skip memory) because "not set" would be a guess.

The environment is read when `read` runs, not when the object is built, so constructing it does no I/O (``AGENTS.md``
§2.6).
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from syncintosh.core.agent_projects import (
    AgentProject,
    AgentProjectEntry,
    AgentProjectMap,
    AgentProjectSpec,
    claude_project_dir_name,
)
from syncintosh.core.ports import DenyList

# The environment variables that move or rename the agent's data, by agent kind.
OVERRIDE_VARIABLES: dict[str, tuple[str, ...]] = {
    "claude": (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_PROJECT_DIR_NAME",
        # Both move where memory is kept, like `autoMemoryDirectory` does.
        "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
        "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    )
}

# A settings file is a few KiB. A bound keeps a planted or runaway file from being read whole into memory.
MAX_SETTINGS_BYTES = 1024 * 1024

# Where the key may be set: the user's settings, and the project's shared and local ones.
_USER_SETTINGS = ".claude/settings.json"
_PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
_MEMORY_KEY = "autoMemoryDirectory"


class SystemAgentProjectIndex:
    """Resolves mapped projects under `home`; constructing it does no I/O."""

    def __init__(self, home: str, deny: DenyList, environ: Mapping[str, str] | None = None) -> None:
        self._home = home
        self._deny = deny
        self._environ = environ

    def read(self, spec: AgentProjectSpec, projects: tuple[AgentProject, ...]) -> AgentProjectMap:
        """The folder each of `projects` has under `spec`, or the reason it has none."""
        environ = os.environ if self._environ is None else self._environ
        overridden = sorted(name for name in OVERRIDE_VARIABLES[spec.kind] if environ.get(name))
        if overridden:
            reason = f"{', '.join(overridden)} is set, so where the agent keeps project data is not known"
            return AgentProjectMap.create(
                tuple(AgentProjectEntry.unresolved(name=each.name, root=each.path, reason=reason) for each in projects)
            )
        user_moved = self._memory_relocated(_USER_SETTINGS)
        return AgentProjectMap.create(tuple(self._entry(each, user_moved) for each in projects))

    def _entry(self, project: AgentProject, user_moved: bool) -> AgentProjectEntry:
        unsafe = self._unsafe(project.path)
        if unsafe is not None:
            return AgentProjectEntry.unresolved(name=project.name, root=project.path, reason=unsafe)
        real = os.path.realpath(Path(self._home) / project.path)
        moved = user_moved or any(self._memory_relocated(f"{project.path}/{name}") for name in _PROJECT_SETTINGS)
        return AgentProjectEntry.resolved(
            name=project.name, root=project.path, directory=claude_project_dir_name(real), memory_relocated=moved
        )

    def _unsafe(self, root: str) -> str | None:
        """Why the mapped root may not be used, or `None` when it is a safe directory."""
        path = Path(self._home) / root
        try:
            if path.is_symlink():
                return "the project's root is a symbolic link"
            if not path.is_dir():
                return "the project is not on this machine at the mapped path"
        except OSError:
            return "the project's root could not be checked"
        if self._deny.is_denied(self._home, root):
            return "the project's root is a protected path"
        if self._deny.escapes(self._home, f"{root}/x"):
            return "the project's root leaves the home directory"
        return None

    def _memory_relocated(self, relative: str) -> bool:
        """True when the settings file at `relative` sets the memory directory, or cannot be read to say it does not."""
        path = Path(self._home) / relative
        try:
            # A settings file kept in a dotfiles repo is a link to a regular file, and that is the file Claude reads:
            # follow it, but read at most the bound whatever the target turns out to be.
            status = path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if not stat.S_ISREG(status.st_mode):
            return True
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                return True
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            return True
        return isinstance(document, dict) and document.get(_MEMORY_KEY) not in (None, "")


__all__ = ["MAX_SETTINGS_BYTES", "OVERRIDE_VARIABLES", "SystemAgentProjectIndex"]
