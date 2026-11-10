# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `codex-sessions` preset: Codex's per-project session rollouts, so `codex resume <id>` works on another
Mac (issue #56).

Step 0 evidence (checked 2026-11-09 against `openai/codex` at tag rust-v0.157.1, which matches the installed
codex-cli 0.157.1; paths relative to `codex-rs/`; names, sizes and record keys only — no rollout content was read):

```
App:       Codex CLI 0.157.1      Bundle ID / Download / Cask: — (a CLI)
Paths:     ~/.codex/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<thread-uuid>.jsonl     (rollout_file_name.rs)
           ~/.codex/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<thread-uuid>_<rollout-uuid>.jsonl
                                                                                           (a reverted thread)
           keyed by DATE, not project; the project is only in the first record: `session_meta` -> payload.cwd
Format:    text, append-only (opened in append mode). A paginated thread stores the byte offset it has indexed in sqlite
           and fails with "durable rollout shrank" if the file shrinks, so a file is only ever extended here.
           Exceptions Codex itself makes: archive (moves the file to `archived_sessions/<file>`), revert (a NEW rollout
           file), `.jsonl.zst` compression and paginated migration (both off by default).
Resume:    `codex resume <id>` finds a rollout by live writer, then a state-DB row, then a scan of `sessions/**` by file
           name, and seeds the DB when it finds one. The PICKER reads the DB only, so a copied rollout is not listed
           until it has been resumed once, even with `--all`: sync output prints `codex resume <id>` for each one.
Identity:  `SessionMeta.creator_user_id` / `creator_account_id` are Option and nothing compares them with the logged-in
           account. They are KEPT (decision D7): dropping them would change line 1, which sqlite has already indexed
           on the machine that wrote it. They are account identifiers, not credentials, and travel encrypted.
History:   `session_meta.history_base.thread_id` is the ROLLOUT id of an immutable prefix file (a fork or a revert).
           A rollout is only resumable where that ancestor file is also present, so apply says when it is not.
Open:      `thread-writer-locks/<thread-id>.lock` is an exclusive flock held by the writer; its presence proves nothing
           (it survives a crash). Probed the way Codex does (`writer_lock.rs`): flock `.coordination.lock`, try-lock
           `<id>.lock`, release at once. Rust's `File::try_lock` is flock(2) on macOS.
Never:     ~/.codex/auth.json, installation_id, logs_*.sqlite*, state_*.sqlite*, history.jsonl, shell_snapshots/,
           thread-writer-locks/, app-server-*, ipc/, archived_sessions/ (deny-listed or never listed).
Env:       CODEX_HOME moves the tree: the layout is refused and reported.
```

The paths are **logical** (`.codex/sessions/@<name>/YYYY/MM/DD/<file>`) and the preset is listed per project in this
machine's `agent_projects`. On disk there is no project folder, so a record's `@<name>` is dropped when it resolves.
The files are enumerated per session by `CodexSessionStore`, never by a glob; the patterns below say which paths a
record may carry. `{year}` and `{2d}` are exactly four and two digits and `{uuid}` is one lowercase UUID, all matched
as written, so a record cannot name another folder under `~/.codex/sessions` or any `.zst` file.
"""

from __future__ import annotations

from syncintosh.core.agent_projects import AgentProjectSpec
from syncintosh.core.presets import Preset, PresetFile

CODEX_PROJECTS = AgentProjectSpec.create(home=".codex/sessions", kind="codex")

_STAMP = "rollout-{year}-{2d}-{2d}T{2d}-{2d}-{2d}"

# Relative to `~/.codex/sessions`: the date folders, then an ordinary rollout or a reverted one.
ROLLOUT_FILES: tuple[str, ...] = (
    f"{{year}}/{{2d}}/{{2d}}/{_STAMP}-{{uuid}}.jsonl",
    f"{{year}}/{{2d}}/{{2d}}/{_STAMP}-{{uuid}}_{{uuid}}.jsonl",
)

CODEX_SESSIONS = Preset.create(
    name="codex-sessions",
    files=tuple(
        PresetFile.create(path=CODEX_PROJECTS.template_path(relative), format="text", append_only=True)
        for relative in ROLLOUT_FILES
    ),
    agent_projects=CODEX_PROJECTS,
)

__all__ = ["CODEX_PROJECTS", "CODEX_SESSIONS", "ROLLOUT_FILES"]
