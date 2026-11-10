# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `claude-sessions` preset: Claude Code's per-project session history, so `claude --resume` works on
another Mac (issue #56).

Step 0 evidence (checked 2026-11-09 against the shipped Claude Code 2.1.308 binary's strings and the official docs;
file names, sizes and record keys only — no transcript content was read):

```
App:       Claude Code 2.1.308 (CLI)      Bundle ID / Download / Cask: — (a CLI)
Paths:     ~/.claude/projects/<dir>/<uuid>.jsonl
           ~/.claude/projects/<dir>/<uuid>/subagents/agent-*.{jsonl,meta.json,forked-skill.json,forked-skill.marker.json}
           ~/.claude/projects/<dir>/<uuid>/tool-results/*
           <dir> = the project's real absolute path, non-alphanumerics -> "-" (core/agent_projects.py)
Format:    text, append-only: an idle transcript only grows; the in-place edits are a tail truncation while a message
           streams (an open session) and opt-in compaction (`-p`/SDK with CLAUDE_CODE_TRANSCRIPT_LOCAL_GC)
Resume:    `--resume <id>` looks in the cwd folder, then worktree folders, then every project folder; the picker hides
           a session only when its recorded cwd exists here, is a different directory and encodes to the same name.
           So a relocated session resumes WITHOUT rewriting `cwd`, which is never rewritten (decision D4).
Cleanup:   `cleanupPeriodDays` (default 30) deletes a `.jsonl` older than N days by MTIME, with its `<uuid>/` folder, so
           apply writes with a fresh mtime.
Open:      ~/.claude/sessions/<pid>.json {pid, sessionId, procStart, …}; procStart is `ps -o lstart= -p <pid>` under
           LC_ALL=C TZ=UTC. A file survives a crash, so a session is open only when the pid is alive AND the
           procStart matches.
Never:     ~/.claude.json, session-env/, shell-snapshots/, file-history/, paste-cache/, history.jsonl, cache/, daemon*
           (deny-listed in system/deny_list.py)
Unverified: anthropics/claude-code#58475 reports an empty project-scoped picker after a move that the code does not
           explain; check `claude --resume` on a second Mac before a release.
```

The paths are **logical** (`.claude/projects/@<name>/…`) and the preset is listed per project in this machine's
`agent_projects`. The files are enumerated per session by `ClaudeSessionStore`, never by a glob; the patterns below
say which paths a record may carry, and `{uuid}` is exactly one UUID, so a record cannot name another folder under a
project.
"""

from __future__ import annotations

from syncintosh.core.presets import Preset, PresetFile
from syncintosh.presets.claude_memory import CLAUDE_PROJECTS

# Relative to the project's folder. Everything an idle session holds, and nothing else.
SESSION_FILES: tuple[str, ...] = (
    "{uuid}.jsonl",
    "{uuid}/subagents/agent-*.jsonl",
    "{uuid}/subagents/agent-*.meta.json",
    "{uuid}/subagents/agent-*.forked-skill.json",
    "{uuid}/subagents/agent-*.forked-skill.marker.json",
    "{uuid}/tool-results/*",
)

CLAUDE_SESSIONS = Preset.create(
    name="claude-sessions",
    files=tuple(
        PresetFile.create(path=CLAUDE_PROJECTS.template_path(relative), format="text", append_only=True)
        for relative in SESSION_FILES
    ),
    agent_projects=CLAUDE_PROJECTS,
)

__all__ = ["CLAUDE_SESSIONS", "SESSION_FILES"]
