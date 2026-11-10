# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `claude-memory` preset: Claude Code's per-project auto-memory (`memory/*.md`), by project name.

Step 0 evidence (issue #56, checked 2026-11-09 against the shipped Claude Code 2.1.308 binary's strings and the
official docs; no transcript or memory content was read):

```
App:       Claude Code 2.1.308 (CLI)      Bundle ID / Download / Cask: — (a CLI, nothing to detect or link)
Paths:     ~/.claude/projects/<dir>/memory/MEMORY.md  and  memory/<fact>.md  (an index plus one file per fact)
           <dir> = the project's real absolute path, non-alphanumerics -> "-" (core/agent_projects.py)
Format:    text (Markdown; the line-level three-way merge)
Noise:     none — a memory file has no machine-specific key to strip
Excluded:  none in code; the secret scan is the backstop for a token a note quotes
Relocated: `autoMemoryDirectory` in ~/.claude/settings.json or the project's .claude/settings{,.local}.json moves
           the directory, so the project's memory is skipped (system/agent_project_index.py)
Env:       CLAUDE_CONFIG_DIR / CLAUDE_CODE_PROJECT_DIR_NAME set -> the layout is refused and reported
```

Memory lives under the **canonical git root's** folder and is shared by that repository's worktrees, while a transcript
is keyed by cwd. Version 1 maps a project by the root the person names in `agent_projects`, which is that git root.

The path is **logical**: `.claude/projects/@<name>/memory/*.md`. Nothing is collected unless this machine's config maps
the name to a root and enables this preset for it (decision D5), and a record is written only under a name the config
maps (decision D3). The preset lists the file with the bare marker (`@`) for the project's element, the way a VS Code
profile's file is listed by its default-profile path.
"""

from __future__ import annotations

from syncintosh.core.agent_projects import AgentProjectSpec
from syncintosh.core.presets import Preset, PresetFile

# Shared with the transcripts preset (issue #56, PR C): both live under `.claude/projects/<dir>/`.
CLAUDE_PROJECTS = AgentProjectSpec.create(home=".claude/projects", kind="claude", memory_prefix="memory/")

CLAUDE_MEMORY = Preset.create(
    name="claude-memory",
    files=(PresetFile.create(path=CLAUDE_PROJECTS.template_path("memory/*.md"), format="text"),),
    agent_projects=CLAUDE_PROJECTS,
)

__all__ = ["CLAUDE_MEMORY", "CLAUDE_PROJECTS"]
