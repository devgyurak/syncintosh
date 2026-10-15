# CLAUDE.md

@AGENTS.md

The file above is the canon for this repository — architecture layers, security
invariants (encryption envelope · manifest · secret exclusion), Python/ruff/ty
conventions, and the test rules (unit + integration, no control flow, 90% coverage).
What follows is only what matters on Claude Code.

## Subagents

- `architecture-guard` — catches layer violations, logic leaking into `cli/`, core
  purity breaks, symlink collection, and mutable boundary objects. Call it after touching
  `core/` · `services/` · infrastructure.
- `crypto-auditor` — proves the ciphertext envelope, AAD binding, manifest IDs, and
  secret exclusions are intact. **Must be called** after touching `crypto/`, `system/`
  secret scanning, the manifest, or anything that encrypts or writes plaintext.
- `test-author` — writes pytest that follows the rules (unit + integration, no control
  flow, parametrize, 90% coverage). Call it when coverage drops.

## Skills

- `verify` — run the full gate (format → lint → typecheck → test+coverage) in order.
- `add-preset` — add a built-in preset. **Step 0 is verifying the real config file
  structure, noise keys, and download metadata** before any code.
- `add-adapter` — add a format adapter that round-trips comments and order.
- `commit` — commit-message, PR-title, and branch-name conventions
  (`ADD:`/`FIX:`/`REF:`/`DEL:`/`DOCS:`/`UPT:`). Read before committing.

## Hooks

`.claude/settings.json` wires three hooks; the bodies live in `scripts/agent_hooks/`
and are shared with Codex, Cursor, OpenCode, and Pi.

| Event | Action |
| --- | --- |
| `PreToolUse` | Block reading/editing plaintext work dir, key exports, `~/.ssh`, `~/.aws`, `~/.kube`, `~/.docker/config.json`, `~/Library/Cookies`, `.env`. |
| `PostToolUse` (Write/Edit) | Run `ruff check --fix` + `ruff format` on the edited `*.py`. |
| `PostToolUse` (Write/Edit) | Block a layer-dependency violation in `src/syncintosh/**`. |

If a hook returns `block`, fix the reported problem and retry — do not work around it. Hooks
are best-effort (they only fire on tool edits), so CI is the real gate; the security guard
fails closed.

## Tool use

- Run `uv run …`, never bare `python`/`pip`.
- Use Grep/Glob for search; avoid shell `grep`/`find`.
- Delegate broad exploration to an Explore subagent and take only the conclusion.
- Commit only when the user asks.
