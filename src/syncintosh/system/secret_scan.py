# SPDX-License-Identifier: AGPL-3.0-or-later
"""The secret scan that gates the encrypt boundary.

Every plaintext is scanned **before** it is sealed, so a refusal leaves the repo untouched
(``docs/design/01-sync-model.md``, ``AGENTS.md`` §3.4). The patterns live here and only here.

A refusal names the **location** (the subject and the line), never the matched bytes: an error
message or a log line is exactly where a recovered secret would leak (``AGENTS.md`` §4).
"""

from __future__ import annotations

import re

from syncintosh.core.errors import SecretScanError

# `(label, pattern)` — a high-signal, low-false-positive set. This is a heuristic in front of the
# real gate (CI + review), not a sandbox (``AGENTS.md`` §6); a missed secret is still refused by
# the dangerous-path deny-list and by the user's own review of what gets tracked.
PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    # PEM-encoded private keys — the header alone is enough, no need to match the body.
    ("private key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # AWS access key id; the fixed `AKIA` prefix plus 16 uppercase-alphanumerics.
    ("AWS access key id", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    # GitHub personal access token (classic).
    ("GitHub token", re.compile(rb"\bghp_[0-9A-Za-z]{36}\b")),
    # GitHub fine-grained personal access token.
    ("GitHub fine-grained token", re.compile(rb"\bgithub_pat_[0-9A-Za-z_]{22,}\b")),
    # Slack token, across the bot/user/app/refresh/workspace prefixes.
    ("Slack token", re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    # A URL carrying credentials inline: `scheme://user:password@host`. The colon in the userinfo
    # next to an `@` is rare outside an actual credential, and this is the shape a proxy URL takes
    # in `http.proxy`, a proxy setting in `~/.zshrc`, or a git remote with a token — none of which
    # the patterns above would notice (decision V2). The slashes may be JSON-escaped (`\/`), which
    # the JSONC projection preserves verbatim for an unchanged value, so both spellings are matched.
    ("URL with credentials", re.compile(rb"[A-Za-z][A-Za-z0-9+.\-]*:(?:\\?/){2}[^/@\s:]+:[^/@\s\\]+@")),
)


def _line_number(plaintext: bytes, offset: int) -> int:
    """The 1-based line a byte offset falls on."""
    return plaintext.count(b"\n", 0, offset) + 1


class PatternSecretScanner:
    """A `SecretScanner` over a fixed pattern table."""

    def scan(self, plaintext: bytes, *, subject: str) -> None:
        """Raise `SecretScanError` naming the location when a pattern matches; never the value."""
        for label, pattern in PATTERNS:
            match = pattern.search(plaintext)
            if match is not None:
                line = _line_number(plaintext, match.start())
                raise SecretScanError(f"{subject}: possible {label} at line {line}")


__all__ = ["PATTERNS", "PatternSecretScanner"]
