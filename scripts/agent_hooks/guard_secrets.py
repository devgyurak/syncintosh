#!/usr/bin/env python3
"""Pre-hook that blocks access to syncintosh secrets and plaintext working copies.

syncintosh stores the user's credentials-adjacent configuration and a master key that is
**unrecoverable if leaked**. An agent that reads the plaintext work directory, a key
export, a real secret file, or the login keychain can leak it into the conversation
context or a log.

This guard is **best-effort**: a regex/token heuristic, not a sandbox. It is one line of
defense alongside CI (which blocks committing secrets) and code review. When it cannot
decide, it fails closed.

Usage:
    guard_secrets.py --tool claude|codex|cursor|opencode|pi
    (hook payload JSON on stdin)

Exit behavior: claude/codex/cursor emit a JSON decision and exit 0; opencode/pi exit 2
with a reason on stderr (their plugins turn that into a block).
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hookio import (
    emit_allow,
    emit_deny,
    extract_command,
    extract_paths,
    looks_like_patch,
    parse_args,
    read_payload,
)

# A path under any of these parts is a test fixture, not a live secret. The Docker preset
# tests need ca.pem/cert.pem/key.pem fixtures on disk, and AGENTS.md §3.4 requires them.
FIXTURE_PARTS = ("tests", "fixtures")

# File names that always contain a secret or a private key.
BLOCKED_BASENAMES = {
    ".netrc",
    "credentials",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}
BLOCKED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".age",  # age-encrypted key export
}

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories an agent must never read from or write to.
SECRET_DIRS = {
    (Path.home() / ".ssh").resolve(),
    (Path.home() / ".aws").resolve(),
    (Path.home() / ".kube").resolve(),
    (Path.home() / ".gnupg").resolve(),
    (Path.home() / ".docker" / "contexts").resolve(),
    (Path.home() / "Library" / "Cookies").resolve(),
    # syncintosh's plaintext working copy — outside git, never for an agent to open.
    (Path.home() / ".local" / "share" / "syncintosh").resolve(),
}
# NOTE: ~/.config/syncintosh is intentionally NOT blocked — the design note keeps only
# non-secret values there (remote URL, model name, machine ID). See docs/design/README.md.

# Exact files whose contents are secret-shaped. Docker's config.json holds `auths`, which
# the docker adapter strips in code, so the raw file must not be read.
SECRET_FILES = {
    (Path.home() / ".docker" / "config.json").resolve(),
}

# Verbs that read or execute a file's contents. For a command containing one of these, a
# bare secret marker anywhere in the text is enough to deny (catches `python3 -c "open(...)"`
# and friends). Search verbs (grep/rg/find/git) are excluded so code can be grepped for a
# dangerous-path constant without tripping the guard.
FILE_READ_VERBS = {
    "cat",
    "less",
    "more",
    "head",
    "tail",
    "strings",
    "xxd",
    "hexdump",
    "od",
    "dd",
    "cp",
    "mv",
    "scp",
    "rsync",
    "sqlite3",
    "jq",
    "yq",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "php",
}

# High-signal substrings, scanned only when a FILE_READ_VERB is present.
SECRET_MARKERS = (
    "/.ssh/",
    "/.aws/",
    "/.kube/",
    "/.gnupg/",
    "/.docker/config.json",
    "/Library/Cookies",
    "/id_rsa",
    "/id_ed25519",
    "/id_ecdsa",
    "library/cookies",
)

# Commands that read the login keychain. Agents never need this; the app reads the keychain
# through the `keyring` library at runtime.
KEYCHAIN_RE = re.compile(r"\bsecurity\s+(find-(generic|internet)-password|dump-keychain|export)\b")

# Shell word-prefixes that are transparently skipped when finding the real command word.
PREFIXES = ("sudo", "time", "env", "nice", "nohup", "command", "exec")

# `uv run`/`uvx` options that consume the next token, so the wrapper can be stripped safely.
UV_VALUE_FLAGS = (
    "--with",
    "--python",
    "-p",
    "--directory",
    "--project",
    "--group",
    "--resolution",
    "--index",
    "--extra",
    "--no-project",
)

_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")

REASON = (
    "Blocked: syncintosh secrets and plaintext working copies are off-limits to agents. "
    "This includes ~/.local/share/syncintosh/work/ (plaintext mirror), key exports, "
    "*.key/*.pem, ~/.ssh, ~/.aws, ~/.kube, ~/.docker/config.json (auths), ~/Library/Cookies, "
    ".env, and the login keychain. Reading them risks leaking the user's secrets or the "
    "unrecoverable master key. Use tests/fixtures/ instead, and read the code to learn the "
    "format."
)


def _is_fixture(path: Path) -> bool:
    parts = path.parts
    return any(parts[i] == "tests" and i + 1 < len(parts) and parts[i + 1] == "fixtures" for i in range(len(parts)))


def _is_blocked_path(raw: str) -> bool:
    if not raw:
        return False
    # A shell would expand $VAR; shlex.split leaves the token literal, so expand it here.
    try:
        path = Path(os.path.expandvars(raw)).expanduser()
    except (ValueError, OSError, RuntimeError):
        return False

    # Resolve FIRST and judge by the target: a tests/fixtures/*.pem that is a symlink to
    # ~/.ssh/id_rsa is still a secret read. If resolution fails (NUL byte, symlink loop),
    # fall back to the literal path — never skip the check (fail closed).
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = path
    if resolved in SECRET_FILES or any(resolved == d or d in resolved.parents for d in SECRET_DIRS):
        return True

    # A genuine fixture target is allowed (Docker ca.pem/cert.pem/key.pem live here).
    if _is_fixture(resolved):
        return False

    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return not name.endswith(".example")
    if name in BLOCKED_BASENAMES:
        return True
    return path.suffix.lower() in BLOCKED_SUFFIXES


def _strip_uv(toks: list[str]) -> list[str]:
    """Drop a leading `uv run [options]` / `uvx [options]` so the real command shows through.

    `uv run python -c "open('…/.aws/credentials')"` must be seen as a `python` invocation,
    or the file-read verb scan never fires.
    """
    if not toks or toks[0] not in ("uv", "uvx"):
        return toks
    if toks[0] == "uv":
        if len(toks) < 2 or toks[1] != "run":
            return toks
        toks = toks[2:]
    else:
        toks = toks[1:]
    while toks and toks[0].startswith("-"):
        flag = toks[0].split("=", 1)[0]
        takes_value = flag in UV_VALUE_FLAGS and "=" not in toks[0]
        toks = toks[2:] if takes_value else toks[1:]
    return toks


def _tokens(segment: str) -> list[str]:
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in PREFIXES):
        toks = toks[1:]
    return _strip_uv(toks)


def _blocked_command(command: str) -> str | None:
    """Return the offending token/verb when a shell command reads a secret, else None."""
    for segment in _SEGMENT_SPLIT_RE.split(command):
        toks = _tokens(segment)
        if not toks:
            continue

        # Token resolution: any token that resolves into a secret dir/file or matches a
        # blocked name is a hit (catches `find ~/.ssh -exec cat {} \;`, `jq . ~/.docker/config.json`).
        for token in toks[1:]:
            if token.startswith("-"):
                continue
            if _is_blocked_path(token):
                return token

        if KEYCHAIN_RE.search(segment):
            return "security (keychain read)"

        # File-read verbs: a bare secret marker anywhere is enough.
        if toks[0] in FILE_READ_VERBS:
            lowered = segment.lower()
            for marker in SECRET_MARKERS:
                if marker in lowered:
                    return marker
    return None


def main() -> None:
    tool = parse_args()
    payload = read_payload()
    try:
        for path in extract_paths(payload):
            if _is_blocked_path(path):
                emit_deny(tool, f"{REASON}\n(blocked path: {path})", payload)

        command = extract_command(payload)
        # A Codex apply_patch body is not a shell command: its paths were already checked
        # above via the patch headers, so scanning it as a shell line only adds noise.
        if command and not looks_like_patch(command):
            offender = _blocked_command(command)
            if offender:
                emit_deny(tool, f"{REASON}\n(blocked command: {command.splitlines()[0][:200]})", payload)

        emit_allow(tool)
    except Exception as exc:
        emit_deny(tool, f"{REASON}\n(guard error, failing closed: {exc!r})", payload)


if __name__ == "__main__":
    main()
