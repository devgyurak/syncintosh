# Pi usage

syncintosh supports Pi through the canon, the skills, and one gate extension.

## Files

- `.pi/settings.json` — project settings (Pi reads this; the older `.pi/config.toml` is
  not a Pi file and was removed).
- `.pi/extensions/syncintosh-gates.ts` — the gate adapter. It shells out to the shared
  `scripts/agent_hooks/` bodies, so behavior changes in one place.
- `AGENTS.md` — the canon. Pi reads it.
- `.agents/skills/` — the skills (a symlink to `.claude/skills/`, the single original).

## Project trust is required

Pi loads project-local `.pi/` resources and extensions **only after the project is
trusted**. On the first run Pi asks; approve it (or use `/trust`, or `pi --approve` for a
single run). Until then the extension does not run and the gates are inactive — rely on
CI (`.github/workflows/agents.yml`) and `verify`.

## Subagents

**Pi has no subagents.** The roles in `.claude/agents/` (architecture-guard, crypto-auditor,
test-author) do not apply. To keep the same separation, either run a fresh session and give
it one role from `.claude/agents/<name>.md` as the opening prompt, or just follow
`AGENTS.md` and the skills.

## Minimal setup

```bash
uv sync --all-extras
uv run python scripts/check_agent_config.py
```

## Skills

Invoke a skill or read `.agents/skills/<name>/SKILL.md` directly:

| Skill | When |
| --- | --- |
| `verify` | run the full gate and diagnose failures |
| `add-preset` | add a built-in preset |
| `add-adapter` | add a format adapter |
| `commit` | commit / PR / branch conventions |
