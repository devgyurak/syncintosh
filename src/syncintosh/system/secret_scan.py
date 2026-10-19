# SPDX-License-Identifier: AGPL-3.0-or-later
"""The secret scan that gates the encrypt boundary.

Every plaintext is scanned **before** it is sealed, so a refusal leaves the repo untouched
(``docs/design/01-sync-model.md``, ``AGENTS.md`` §3.4). The patterns live here and only here.

A refusal names the **location** (the subject and the line), never the matched bytes: an error
message or a log line is exactly where a recovered secret would leak (``AGENTS.md`` §4).

The scan needs the file's **format**, because the same bytes mean different things in different
files: a shell file may hold a reference (`http://$USER:$PASS@proxy`) where a JSON settings file has
none, so `$` there is an ordinary character of a value (see `patterns_for`).
"""

from __future__ import annotations

import re

from syncintosh.core.errors import SecretScanError

# `(label, pattern)` — a high-signal, low-false-positive set. This is a heuristic in front of the
# real gate (CI + review), not a sandbox (``AGENTS.md`` §6); a missed secret is still refused by
# the dangerous-path deny-list and by the user's own review of what gets tracked.
COMMON_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
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
)

# A URL carrying credentials inline: `scheme://user:password@host`. The colon in the userinfo next to
# an `@` is rare outside an actual credential, and this is the shape a proxy URL takes in
# `http.proxy`, a proxy setting in `~/.zshrc`, or a git remote with a token — none of which the
# patterns above would notice (decision V2). The slashes may be JSON-escaped (`\/`), which the JSONC
# projection preserves verbatim for an unchanged value, so both spellings are matched.
#
# `%s` is where the password's own rule goes, so the two spellings below cannot drift apart:
#
# * **A structured file** (`jsonc`, `toml`, `yaml`, `xml`, `plist`) has no interpolation, so a
#   password may be anything — `pa$$w0rd`, `$ecretPass`, `abc{123}` are values like any other. A
#   password that *merely starts* with `$` is still a literal here.
# * **A shell or plain-text file** may hold a reference, which is the recommended way to keep a
#   secret out of a dotfile; refusing it would block the file precisely when its owner did the right
#   thing, and a variable name is not a value. A reference inside a single-quoted string
#   (`proxy='http://u:$PASS@h'`) or behind a backslash is *not* expanded by the shell, so the text
#   itself is the credential — hence a second pattern for that case rather than a wider skip.
#
# A password that is matched as a literal may not contain `,` or `"`: those are what sits between
# two values in a settings file or a `NO_PROXY` list, and a line like
# `{"proxy":"http://h:3128","email":"a@b.c"}` otherwise reads the text between the port and the
# email's `@` as a credential — a false positive that would stop every tracked file on the machine
# from syncing. `?` and `#` are not needed for that and stay matchable, since a query or a fragment
# can be part of a real URL.
_URL_CREDENTIALS = rb"[A-Za-z][A-Za-z0-9+.\-]*:(?:\\?/){2}[^/@\s:]*:%s[^/@\s,\"]+@"

# A URL the shell passes through verbatim, so its password is a literal and may hold anything a URL
# may — `,`, `?`, `#`, and `"` included. `;` and `'` stay out: they end the quoted string, and
# `m='http://h:3128';me@x.com` would otherwise read `3128';me` as the password.
_URL_QUOTED_CREDENTIALS = rb"[A-Za-z][A-Za-z0-9+.\-]*:(?:\\?/){2}[^/@\s:]*:[^/@\s;']+@"

# A password that is only a reference, in a file where the shell would expand it.
_REFERENCE_PASSWORD = rb"(?!\$\{?[A-Za-z_]\w*\}?@)"

# Only `text` is read by a shell, and only there does a `$` in a URL mean a reference rather than a
# character of a value. Every other format — including one this build has never heard of — gets the
# strict table, which is the fail-closed direction: a literal credential is refused, not skipped.
_SHELL_FORMATS = ("text",)

# A structured file: any password, `$` and `{` included.
STRUCTURED_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    *COMMON_PATTERNS,
    ("URL with credentials", re.compile(_URL_CREDENTIALS % rb"")),
)

# A shell file. Two spellings, because the shell decides what a `$` means by the quoting around it:
# a URL the shell would expand takes the skip rule, and one it passes through verbatim is matched as
# the literal it is.
SHELL_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    *COMMON_PATTERNS,
    # The lookbehind also rules out a start *inside* a scheme: without the `[A-Za-z0-9+.\-]` there,
    # `m='http://h:3128';me@x` matched from `ttp`, where the preceding character is `h` and the quote
    # rule no longer applied.
    (
        "URL with credentials",
        re.compile(rb"(?<![A-Za-z0-9+.\-\\'])" + _URL_CREDENTIALS % _REFERENCE_PASSWORD),
    ),
    ("URL with credentials", re.compile(rb"(?<=[\\'])" + _URL_QUOTED_CREDENTIALS)),
)


def patterns_for(format: str) -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    """The pattern table for a file's format: the shell one only where a shell reads the text."""
    return SHELL_PATTERNS if format in _SHELL_FORMATS else STRUCTURED_PATTERNS


def _line_number(plaintext: bytes, offset: int) -> int:
    """The 1-based line a byte offset falls on."""
    return plaintext.count(b"\n", 0, offset) + 1


class PatternSecretScanner:
    """A `SecretScanner` over a fixed pattern table."""

    def scan(self, plaintext: bytes, *, subject: str, format: str) -> None:
        """Raise `SecretScanError` naming the location when a pattern matches; never the value.

        `format` picks the URL table: a shell file may reference a credential, a structured file may
        not (see `patterns_for`).
        """
        for label, pattern in patterns_for(format):
            match = pattern.search(plaintext)
            if match is not None:
                line = _line_number(plaintext, match.start())
                raise SecretScanError(f"{subject}: possible {label} at line {line}")


__all__ = ["COMMON_PATTERNS", "PatternSecretScanner", "patterns_for"]
