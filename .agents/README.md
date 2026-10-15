# Agent configuration map

This repository supports five coding agents. **The canon is the single root
`AGENTS.md`.** Every tool converges there from its own entrypoint. To change a rule, edit
`AGENTS.md` — the files below are per-tool wiring.

| Tool | Reads | Note |
| --- | --- | --- |
| **Codex** | `AGENTS.md` + `.agents/skills/`<sup>※</sup> + `.codex/` | Subagents in `.codex/agents/*.toml`, hooks in `.codex/hooks.json` |
| **Claude Code** | `CLAUDE.md` → `@AGENTS.md` + `.claude/skills/` + `.claude/settings.json` | Subagents in `.claude/agents/` |
| **Pi** | `AGENTS.md` + `.agents/skills/` + `.pi/settings.json` | Gates via `.pi/extensions/` (project trust required); **no subagents** |
| **Cursor** | `AGENTS.md` + `.cursor/rules/*.mdc` + `.cursor/hooks.json`<sup>※</sup> | `.mdc` files are three scope-focused highlights |
| **OpenCode** | `AGENTS.md` + `.opencode/agents/` + `.opencode/opencode.json` | Gates run through `.opencode/plugin/syncintosh-gates.js` (also read from `.opencode/plugins/`) |

<sup>※</sup> **Unverified.** `SKILL.md`, Cursor hooks, and Codex hooks are read by their
tools per the tools' own docs, but only Claude Code's loading has been observed in this
repository. Since the canon lives in `AGENTS.md` — which all five read — the rules reach
an agent even if a given tool cannot open a skill or run a hook.

Skills are **not** duplicated per tool. The one thing that is duplicated is the
subagents, because `.opencode/agents/` and `.codex/agents/` use different frontmatter
schemas.

## File layout

```
AGENTS.md                       ← canon (read directly by Codex · Pi · Cursor · OpenCode)
CLAUDE.md                       ← imports @AGENTS.md + Claude-specific notes

.claude/
├── agents/                     ← Claude Code subagents
│   ├── architecture-guard.md
│   ├── crypto-auditor.md
│   └── test-author.md
├── settings.json               ← Claude hooks
└── skills/                     ← skill originals (shared by all five tools, SKILL.md standard)
    ├── verify/SKILL.md
    ├── commit/SKILL.md
    ├── add-preset/SKILL.md
    └── add-adapter/SKILL.md

.agents/
├── README.md                   ← this file
└── skills → ../.claude/skills  ← symlink (Codex · Pi read .agents/skills)

.codex/
├── config.toml
├── hooks.json
└── agents/                     ← same subagents, Codex schema
    ├── architecture_guard.toml
    ├── crypto_auditor.toml
    └── test_author.toml

.opencode/
├── opencode.json
├── plugin/syncintosh-gates.js  ← hook adapter → scripts/agent_hooks/
└── agents/                     ← same subagents, OpenCode schema
    ├── architecture-guard.md
    ├── crypto-auditor.md
    └── test-author.md

.cursor/
├── hooks.json
└── rules/
    ├── architecture.mdc        ← alwaysApply: true
    ├── python-style.mdc        ← globs: src/**/*.py
    └── testing.mdc             ← globs: tests/**/*.py

.pi/
├── README.md
├── settings.json               ← project settings (trust required to load)
└── extensions/
    └── syncintosh-gates.ts     ← gate adapter → scripts/agent_hooks/ (trust required)

scripts/
├── check_agent_config.py       ← integrity check (CI gate)
└── agent_hooks/                ← hook bodies (shared by Claude · Codex · Cursor · OpenCode · Pi)
    ├── _hookio.py
    ├── guard_secrets.py
    ├── format_python.py
    ├── check_layering.py
    └── selftest.py
```

## Subagents

All three exist in `.claude/agents/`, `.opencode/agents/`, and `.codex/agents/`. The body
is the same; only the frontmatter schema differs.

- **architecture-guard** (read-only) — layer violations, logic in `cli/`, core purity,
  symlink collection, mutable boundary objects. After `core/` · `services/` · adapters.
- **crypto-auditor** (read-only) — proves the envelope, AAD, manifest IDs, and secret
  exclusions are intact. After `crypto/` · `system/` secret scan · manifest · encryption.
- **test-author** (writable) — writes pytest to spec. When coverage drops below 90%.

## Skills

- **verify** — run the whole gate and diagnose failures
- **add-preset** — add a built-in preset (verify real structure → collector → adapter →
  merge/ignore → tests → docs)
- **add-adapter** — add a format adapter that round-trips comments and order
- **commit** — commit/PR/branch conventions and prefix boundaries

## Hooks

The bodies live once in `scripts/agent_hooks/`. Each tool’s hook config calls the same
script with a `--tool` flag, so behavior changes in one place.

| Hook | Where | Does |
| --- | --- | --- |
| `guard_secrets.py` | Pre | Blocks secret/plaintext/keychain access |
| `format_python.py` | Post | `ruff check --fix` + `ruff format` on edited `*.py`, and surfaces errors it could not fix |
| `check_layering.py` | Post | Blocks layer-dependency violations under `src/syncintosh/`. Cursor runs it on the `stop` hook, scanning changed files via `git status`. |

Enforcement is **best-effort**: it only fires for a file an agent edits through a wired tool.
A change made by `Bash`/`sed`, or in a harness whose hook is not loaded, skips it — so every
rule that matters is also a pytest test that CI runs (AGENTS.md §7). The security guard
fails **closed** (claude/codex/cursor emit a deny; opencode/pi exit non-zero; Cursor sets
`failClosed: true`).

## Maintenance

- **To change a rule, edit `AGENTS.md`.** All five tools follow.
- **Change a subagent in all three copies** (`.claude/` · `.opencode/` · `.codex/`). The
  body must match. `scripts/check_agent_config.py` verifies this — Codex
  `developer_instructions` must equal the Claude body verbatim.
- **Subagents do not exist on Pi.** The three roles apply to Claude Code, OpenCode, and
  Codex; on Pi, read the role file as a fresh session's opening prompt instead.
- **Edit a skill only in `.claude/skills/`** — Codex·Pi see the same file via the
  `.agents/skills` symlink. On a checkout that cannot follow the symlink, skill discovery
  is empty there but `AGENTS.md` still works.
- **Do not replicate `AGENTS.md` into `.cursor/rules/`.** Cursor reads `AGENTS.md`;
  `.mdc` files are **scope-focused highlights** only. A glob-independent rule copied into
  an `.mdc` just drifts from the canon.
- **Rule vs. skill**: always-true rules (layers, security invariants) belong in
  `AGENTS.md`; a procedure needed only for a specific task (adding a preset, committing)
  belongs in a skill. `AGENTS.md` is loaded into every session; skills load on demand.
- **After editing agent config, run** `uv run python scripts/check_agent_config.py`.
  CI (`.github/workflows/agents.yml`) runs the same check plus the hook self-tests.
