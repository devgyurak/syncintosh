# 07 — Coding-agent session history

Status: Accepted (issue #56). This note covers the machinery every agent shares; each agent's layout is in
`presets.md`.

A coding agent's session transcript (Claude Code, Codex) is **data, not settings**, and it breaks three assumptions the
settings presets rely on: it is append-only, it is unique per session, and it can be tens of megabytes and carry
whatever the person pasted. This note records how it is carried between Macs so that `claude --resume` and
`codex resume` work on the other one.

## The item kind

A transcript is a `text` file with `PresetFile.append_only` set. There is **no manifest change**: `FileFormat` stays a
closed set, and an old build that does not know the preset refuses the record at the catalog check (fail closed). The
flag lives in the local catalog only and, like `arrays`, is not in the policy digest, because it changes how two copies
are compared, not which bytes an object holds. `PresetFile.create` refuses it with a keyed format, `binary`, noise or
excluded keys, an ignore list, a syntax check, an allowlist, or array rules; `Preset.create` refuses it beside profiles,
a config directory or documents.

## The prefix rule (`core/append.py`)

Two copies of one file relate in one of three ways, and no base is needed:

| Local vs repo | Result |
|---|---|
| equal | keep |
| one is a byte prefix of the other | the longer wins (collect takes a local extension; apply writes a repo extension) |
| neither | **forked**: the repository's copy is kept, the local one is not collected, and it is reported — the cycle is never blocked |

The rule is pure and symmetric; hypothesis properties pin idempotence, that an extension always wins whichever side it
is on, and that a fork is exactly "neither is a prefix".

## Sessions, not files (`core/sessions.py`)

A session is a transcript plus the files beside it (sub-agent transcripts, spilled tool results). The unit is the whole
session: it is sealed, skipped, or refused together, because a session with a file missing cannot be resumed.

* **Enumeration** is a port (`SessionStore`), not a glob: `PresetGlob` patterns one element and stops at
  `MAX_GLOB_EXPANSION`, which a real project exceeds. The store reports names, sizes and mtimes — never content.
* **Idle.** A session is sealed only when every file is older than 30 minutes **and** the agent does not have it open
  (`SessionActivity`, the agent's own signal). A session whose state cannot be told counts as open. Re-sealing a growing
  file on every sync would commit a full new ciphertext each time; waiting for idle bounds that.
* **Caps.** 48 MiB of plaintext per file (the sealed object stays under GitHub's 50 MiB warning) and 256 MiB per
  project. A file over its cap, or the newest new sessions past the project cap, are skipped and reported; there is no
  chunking. The project cap is soft like a glob's: a session that is already a record is never dropped by it.
* **Secrets.** Before anything of a session is staged, every file is scanned. One hit leaves the **whole session** out
  and reports the session id, the file and the scanner's own location text (a label and a line number — never the
  value). The rest of the cycle goes on. There is no redaction: it would alter history. `write_repo`'s scan stays as the
  second wall (``AGENTS.md`` §3.4).

## Apply

A session file is written only when it is missing, or when the repo's copy **strictly extends** the local one. The old
bytes are then a prefix of the new, so no restore point is made (the exception to the overwrite rule, noted in
`01-sync-model.md`). The write gets a fresh mtime, which matters: Claude Code's cleanup deletes a `.jsonl` older than
`cleanupPeriodDays` by mtime, so a copy that kept the source's mtime could be reaped on the next start.

Never written: a session the agent has open or that cannot be told, a local file that is longer or has forked, and a file
the person removed here (the suppressed rule). A file edited between collect and apply aborts the write.

## Reporting

`SyncReport.session_skipped` carries one row per session and reason (`secret`, `cap`, `forked`, `open`, `unknown`), each
with a fixed phrase. None blocks the cycle, and none carries a transcript value.

## Per agent

Where a transcript lives, how a project is identified across Macs, and how a session's open state is read are per agent;
`core` and `services` see only `SessionStore` and `SessionActivity` (`system/claude_sessions.py`,
`system/codex_sessions.py`, combined by `system/agent_sessions.py`).

| | Claude Code | Codex |
|---|---|---|
| Layout | `~/.claude/projects/<dir>/<uuid>.jsonl` + `<uuid>/subagents/`, `tool-results/` | `~/.codex/sessions/YYYY/MM/DD/rollout-…-<thread>.jsonl` |
| Project | the folder name, derived from the project's real path | the first record's `cwd`, or an already-published record |
| Logical path | `.claude/projects/@<name>/…` | `.codex/sessions/@<name>/YYYY/MM/DD/…` (the `@<name>` is dropped on disk) |
| Open | `~/.claude/sessions/<pid>.json` + `procStart` | flock on `thread-writer-locks/<thread>.lock` |
| Not written over | — | a rollout archived or compressed here |
| Said after a write | a CLI version difference | `codex resume <id>`, a missing `history_base`, a CLI version difference |

`cwd` inside a record is never rewritten. Relocation is enough for Claude Code (`--resume` searches every project folder;
the picker hides a session only when its recorded `cwd` exists here, differs and encodes to the same folder name) and for
`codex resume <id>` (it finds a rollout by file name). Codex's `creator_user_id` and `creator_account_id` are kept: they
are account identifiers, not credentials, and removing them would change line 1, which the database has already indexed.
