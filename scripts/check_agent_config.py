#!/usr/bin/env python3
"""Integrity check for the syncintosh agent configuration.

Verifies that the canon, the five harness entrypoints, the subagents, the skills, and the
hooks stay consistent — a broken pointer or a schema/body mismatch should fail here (and in
CI), not on a user's machine.

Standard library only. `tomllib` requires Python 3.11+.

    uv run python scripts/check_agent_config.py

Checks:
  1. Canon files exist (AGENTS.md, CLAUDE.md, .agents/README.md, per-tool configs).
  2. `.claude/skills/*/SKILL.md` has `name` (== dir) and `description`.
  3. `.claude/agents/*.md` has `name`, `description`, `tools`.
  4. `.opencode/agents/*.md` matches the Claude set and has an identical body.
  5. `.codex/agents/*.toml` matches the Claude set and has an identical body.
  6. `.cursor/rules/*.mdc` has `description`, and at least one is `alwaysApply: true`.
  7. Every hook config references an existing `scripts/agent_hooks/*.py`.
  8. `.agents/skills` resolves to `.claude/skills`.
  9. The subagent names appear in both CLAUDE.md and .agents/README.md.
 10. `.pi/settings.json` is valid JSON and the Pi extension + OpenCode plugin exist.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAILURES: list[str] = []

HOOK_SCRIPT_RE = re.compile(r"scripts/agent_hooks/([A-Za-z0-9_]+\.py)")

CANON_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".agents/README.md",
    ".claude/settings.json",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".cursor/hooks.json",
    ".opencode/opencode.json",
    ".opencode/plugin/syncintosh-gates.js",
    ".pi/settings.json",
    ".pi/extensions/syncintosh-gates.ts",
)

HOOK_CONFIGS = (
    ".claude/settings.json",
    ".codex/hooks.json",
    ".cursor/hooks.json",
)

# Non-JSON hook adapters that still call the shared scripts.
HOOK_ADAPTERS = (
    ".opencode/plugin/syncintosh-gates.js",
    ".pi/extensions/syncintosh-gates.ts",
)


def fail(message: str) -> None:
    FAILURES.append(message)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (top-level frontmatter keys, body after the frontmatter block)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    keys: dict[str, str] = {}
    end = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            end = index
            break
        if line[:1] not in (" ", "\t") and ":" in line:
            key, _, value = line.partition(":")
            keys[key.strip()] = value.strip()
    if end is None:
        return {}, text
    return keys, "\n".join(lines[end + 1 :]).strip()


def check_canon() -> None:
    for rel in CANON_FILES:
        if not (ROOT / rel).is_file():
            fail(f"canon file missing: {rel}")

    settings = ROOT / ".pi" / "settings.json"
    if settings.is_file():
        try:
            json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f".pi/settings.json is not valid JSON: {exc}")

    # The old Pi config filename is not a Pi file; make sure it does not creep back.
    if (ROOT / ".pi" / "config.toml").exists():
        fail(".pi/config.toml exists — Pi reads .pi/settings.json, not config.toml")


def check_skills() -> None:
    skill_dir = ROOT / ".claude" / "skills"
    dirs = sorted(skill_dir.glob("*/SKILL.md"))
    if not dirs:
        fail("no skills found in .claude/skills/")
    for skill in dirs:
        keys, _ = parse_frontmatter(skill.read_text(encoding="utf-8"))
        if keys.get("name") != skill.parent.name:
            fail(f"skill name mismatch: {skill} has name={keys.get('name')!r}, dir={skill.parent.name!r}")
        if not keys.get("description"):
            fail(f"skill missing description: {skill}")


def _claude_agents() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    agents: dict[str, dict[str, str]] = {}
    bodies: dict[str, str] = {}
    for path in sorted((ROOT / ".claude" / "agents").glob("*.md")):
        keys, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        name = keys.get("name")
        if not name:
            fail(f"claude agent missing name: {path}")
            continue
        for required in ("description", "tools"):
            if not keys.get(required):
                fail(f"claude agent {name} missing {required}")
        agents[name] = keys
        bodies[name] = body
    if not agents:
        fail("no claude agents found in .claude/agents/")
    return agents, bodies


def check_opencode_agents(claude: dict[str, dict[str, str]], bodies: dict[str, str]) -> None:
    oc_dir = ROOT / ".opencode" / "agents"
    seen: set[str] = set()
    for path in sorted(oc_dir.glob("*.md")):
        keys, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if not keys.get("description"):
            fail(f"opencode agent missing description: {path}")
        if keys.get("mode") != "subagent":
            fail(f"opencode agent missing `mode: subagent`: {path}")
        seen.add(path.stem)
    if seen != set(claude):
        fail(f"opencode agent set {sorted(seen)} != claude agent set {sorted(claude)}")
    for name in claude:
        oc_path = oc_dir / f"{name}.md"
        if not oc_path.is_file():
            continue
        _, oc_body = parse_frontmatter(oc_path.read_text(encoding="utf-8"))
        if oc_body != bodies[name]:
            fail(f"opencode body for {name} differs from the claude body (bodies must match)")


def check_codex_agents(claude: dict[str, dict[str, str]], bodies: dict[str, str]) -> None:
    cx_dir = ROOT / ".codex" / "agents"
    expected = {name.replace("-", "_") for name in claude}
    seen: set[str] = set()
    for path in sorted(cx_dir.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        name = data.get("name")
        if not name:
            fail(f"codex agent missing name: {path}")
            continue
        seen.add(name)
        for required in ("description", "developer_instructions"):
            if not data.get(required):
                fail(f"codex agent {name} missing {required}")
        # Body parity: developer_instructions must be the Claude body verbatim.
        claude_name = name.replace("_", "-")
        if claude_name in bodies and data.get("developer_instructions", "").strip() != bodies[claude_name]:
            fail(f"codex body for {name} differs from the claude body (bodies must match)")
    if seen != expected:
        fail(f"codex agent set {sorted(seen)} != expected {sorted(expected)}")


def check_cursor_rules() -> None:
    rules = sorted((ROOT / ".cursor" / "rules").glob("*.mdc"))
    if not rules:
        fail("no cursor rules found in .cursor/rules/")
        return
    always = 0
    for rule in rules:
        keys, _ = parse_frontmatter(rule.read_text(encoding="utf-8"))
        if not keys.get("description"):
            fail(f"cursor rule missing description: {rule}")
        if keys.get("alwaysApply") == "true":
            always += 1
    if always == 0:
        fail("no cursor rule sets `alwaysApply: true` (the architecture rule should)")


def _collect_hook_scripts(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _collect_hook_scripts(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_hook_scripts(item, found)
    elif isinstance(node, str):
        found.update(HOOK_SCRIPT_RE.findall(node))


def check_hooks() -> None:
    referenced: set[str] = set()
    for rel in HOOK_CONFIGS:
        path = ROOT / rel
        if path.is_file():
            _collect_hook_scripts(json.loads(path.read_text(encoding="utf-8")), referenced)

    for rel in HOOK_ADAPTERS:
        path = ROOT / rel
        if path.is_file():
            referenced.update(HOOK_SCRIPT_RE.findall(path.read_text(encoding="utf-8")))
        else:
            fail(f"hook adapter missing: {rel}")

    if not referenced:
        fail("no hook scripts referenced by any hook config")

    for name in sorted(referenced):
        if not (ROOT / "scripts" / "agent_hooks" / name).is_file():
            fail(f"hook config references a missing script: scripts/agent_hooks/{name}")

    # Every hook body that is a real script should be wired somewhere.
    for script in (ROOT / "scripts" / "agent_hooks").glob("*.py"):
        if script.name.startswith("_") or script.name == "selftest.py":
            continue
        if script.name not in referenced:
            fail(f"hook script not wired into any hook config: scripts/agent_hooks/{script.name}")


def check_skills_symlink() -> None:
    link = ROOT / ".agents" / "skills"
    if not link.exists():
        fail(".agents/skills is missing (Codex/Pi read it; expected a symlink to ../.claude/skills)")
        return
    if not link.is_symlink():
        fail(".agents/skills is not a symlink; it should point to ../.claude/skills")
        return
    if link.resolve() != (ROOT / ".claude" / "skills").resolve():
        fail(f".agents/skills resolves to {link.resolve()}, expected .claude/skills")


def check_names_documented(claude: dict[str, dict[str, str]]) -> None:
    for doc in ("CLAUDE.md", ".agents/README.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for name in claude:
            if name not in text:
                fail(f"{doc} does not mention subagent {name!r}")


def main() -> int:
    check_canon()
    check_skills()
    claude, bodies = _claude_agents()
    check_opencode_agents(claude, bodies)
    check_codex_agents(claude, bodies)
    check_cursor_rules()
    check_hooks()
    check_skills_symlink()
    check_names_documented(claude)

    if FAILURES:
        print("agent config check FAILED:", file=sys.stderr)
        for failure in FAILURES:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("agent config check: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
