"""Shared I/O helper for the syncintosh agent hooks.

One hook body serves five harnesses. The stdin payload keys and the output JSON schema
differ slightly per tool, so the differences are absorbed here alone.

- Claude Code : https://code.claude.com/docs/en/hooks
- Codex CLI   : Claude-Code-compatible hook stdin/schema (see .codex/hooks.json).
                `apply_patch` delivers a patch string in `tool_input.command`, so paths
                are read from `*** Add|Update|Delete File:` / `*** Move to:` headers.
- Cursor      : https://cursor.com/docs/agent/hooks   ({"permission": ...}); its after/stop
                hooks consume `agent_message` / `followup_message`.
- OpenCode    : driven by .opencode/plugin/syncintosh-gates.js — a non-zero exit blocks
- Pi          : driven by .pi/extensions/syncintosh-gates.ts — `tool_call` return
                `{ block, reason }`; a non-zero exit from this script signals block

Deny/block protocol per tool:
- claude/codex: print a JSON decision object and exit 0
- cursor:       print a JSON object ({"permission":"deny"} | {"followup_message":...}) and exit 0
- opencode/pi:  write the reason to stderr and exit non-zero (2 = deny, 1 = block)
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

CLAUDE = "claude"
CODEX = "codex"
CURSOR = "cursor"
OPENCODE = "opencode"
PI = "pi"

# Tools that block through the process exit code rather than a JSON decision object.
EXIT_CODE_TOOLS = frozenset({OPENCODE, PI})

_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_MOVE_RE = re.compile(r"^\*\*\*\s+Move to:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_MARKERS = ("*** Begin Patch", "*** Add File:", "*** Update File:", "*** Delete File:")


def parse_args() -> str:
    """Read `--tool <name>`. Defaults to the Claude schema when absent."""
    return parse_option("--tool", CLAUDE) or CLAUDE


def parse_option(name: str, default: str | None = None) -> str | None:
    """Read a `--name value` / `--name=value` option from argv, else ``default``."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return default


def read_payload() -> dict[str, Any]:
    """Read the hook JSON from stdin. Empty or malformed input yields an empty dict."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def looks_like_patch(text: str) -> bool:
    """True when a string is a Codex apply_patch body rather than a shell command."""
    return any(marker in text for marker in _PATCH_MARKERS)


def extract_patch_paths(text: str) -> list[str]:
    """File paths a Codex apply_patch body would touch, from its `*** ... File:` headers."""
    if not looks_like_patch(text):
        return []
    return [p for p in (*_PATCH_FILE_RE.findall(text), *_PATCH_MOVE_RE.findall(text)) if p]


def _walk(node: Any, keys: tuple[str, ...], out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str) and value:
                out.append(value)
            else:
                _walk(value, keys, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, keys, out)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def extract_paths(payload: dict[str, Any]) -> list[str]:
    """Collect every file path the tool call touches, regardless of key spelling.

    Claude Code : tool_input.file_path / tool_input.edits[].file_path / tool_input.notebook_path
    Codex CLI   : apply_patch body → `*** Add|Update|Delete File: <path>` headers
    Cursor      : file_path (afterFileEdit), edits[].path
    OpenCode    : args.filePath / args.path / args.file_path
    Pi          : input.path (read), input.filePath (write/edit)
    """
    found: list[str] = []
    _walk(payload, ("file_path", "filePath", "path", "notebook_path", "target_file"), found)
    ordered = _dedupe(found)
    if ordered:
        return ordered
    return _dedupe(extract_patch_paths(extract_command(payload)))


def extract_command(payload: dict[str, Any]) -> str:
    """Pull the command/patch string a tool is about to run (Bash / apply_patch / shell)."""
    found: list[str] = []
    _walk(payload, ("command",), found)
    return "\n".join(found)


def event_name(payload: dict[str, Any]) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "")


def emit_allow(tool: str) -> None:
    """Pass through with no action."""
    if tool == CURSOR:
        print(json.dumps({"permission": "allow"}, ensure_ascii=False))
    sys.exit(0)


def emit_deny(tool: str, reason: str, payload: dict[str, Any]) -> None:
    """Block the attempted action and return the reason to the model."""
    if tool in EXIT_CODE_TOOLS:
        print(reason, file=sys.stderr)
        sys.exit(2)

    if tool == CURSOR:
        print(
            json.dumps(
                {"permission": "deny", "user_message": reason, "agent_message": reason},
                ensure_ascii=False,
            )
        )
        sys.exit(0)

    # Claude Code / Codex CLI
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name(payload) or "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
                "systemMessage": reason,
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)


def emit_block(tool: str, reason: str) -> None:
    """After an action already ran, tell the model to fix it (PostToolUse / stop)."""
    if tool in EXIT_CODE_TOOLS:
        print(reason, file=sys.stderr)
        sys.exit(1)

    if tool == CURSOR:
        # Cursor's after-edit hook observes; the `stop` hook continues the agent.
        # Emitting both keys covers either binding.
        print(json.dumps({"followup_message": reason, "agent_message": reason}, ensure_ascii=False))
        sys.exit(0)

    print(json.dumps({"decision": "block", "reason": reason, "systemMessage": reason}, ensure_ascii=False))
    sys.exit(0)


def emit_note(tool: str, message: str) -> None:
    """Deliver information without blocking."""
    if tool in EXIT_CODE_TOOLS:
        print(message, file=sys.stderr)
        sys.exit(0)
    if tool == CURSOR:
        print(json.dumps({"agent_message": message}, ensure_ascii=False))
    else:
        print(json.dumps({"suppressOutput": True, "systemMessage": message}, ensure_ascii=False))
    sys.exit(0)
