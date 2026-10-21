# SPDX-License-Identifier: AGPL-3.0-or-later
"""The secret scan that gates the encrypt boundary.

Every plaintext is scanned **before** it is sealed, so a refusal leaves the repo untouched
(``docs/design/01-sync-model.md``, ``AGENTS.md`` §3.4). The patterns live here and only here.

A refusal names the **location** (the subject and the line), never the matched bytes: an error
message or a log line is exactly where a recovered secret would leak (``AGENTS.md`` §4).

The scan needs the file's **format**, because the same bytes mean different things in different
files: a shell file may hold a reference (`http://$USER:$PASS@proxy`) where a JSON settings file has
none, so `$` there is an ordinary character of a value (see `patterns_for`).

The patterns are a heuristic in front of the real gate (``AGENTS.md`` §6), so a widened pattern is
paired with the local allowlist (`system/scan_allow.py`): a false positive is visible, names its
location, and can be allowed on the machine that hit it (#25, #28).
"""

from __future__ import annotations

import re
import shlex

from syncintosh.core.errors import SecretScanError
from syncintosh.system.scan_allow import (
    ALLOWABLE_LABELS,
    AUTHORIZATION_LABEL,
    BASIC_LABEL,
    BEARER_LABEL,
    URL_CREDENTIALS_LABEL,
    ScanAllow,
    fingerprint,
)

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
    # ------------------------------------------------------------ fixed vendor prefixes (issue #28)
    #
    # Vendors whose tokens carry a fixed, documented prefix. The prefix is what makes these
    # high-signal, so each pattern anchors on it and puts a floor (not an exact width) under the
    # body: a vendor lengthens a body without changing the prefix (OpenAI's `sk-proj-` bodies did),
    # and a floor still refuses the token. Sources are the vendors' own key/token docs, checked
    # against gitleaks' default rules for the same vendors.
    ("OpenAI API key", re.compile(rb"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}")),
    # The legacy `sk-<48>` shape has no distinguishing prefix, so it is matched only by the
    # `T3BlbkFJ` marker every such key embeds (the shape gitleaks keys on). A bare `sk-` would
    # collide with DeepSeek, OpenRouter, and every placeholder a tutorial writes.
    ("OpenAI legacy API key", re.compile(rb"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}")),
    ("Anthropic API key", re.compile(rb"\bsk-ant-(?:api03|admin01|oat01)-[A-Za-z0-9_-]{20,}")),
    # GitLab personal access token: `glpat-` plus 20.
    ("GitLab token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}")),
    # npm access token: `npm_` plus 36, the fixed body ending in the appended CRC32.
    ("npm token", re.compile(rb"\bnpm_[A-Za-z0-9]{36}\b")),
    # PyPI API token: a macaroon whose static base64 segment decodes to `pypi.org`.
    ("PyPI token", re.compile(rb"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{10,}")),
    # Docker Hub personal / organization access token.
    ("Docker Hub token", re.compile(rb"\bdckr_(?:pat|oat)_[A-Za-z0-9_-]{20,}")),
    # Hugging Face user access token: `hf_` plus 34.
    ("Hugging Face token", re.compile(rb"\bhf_[A-Za-z0-9]{34}\b")),
    # Google API key: the fixed `AIza` prefix plus 35.
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}")),
    # Stripe secret / restricted key. A publishable (`pk_`) key is not a secret and is left alone.
    ("Stripe key", re.compile(rb"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,}")),
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
# What a literal password may hold is a **fail-closed** choice, not a grammar one. RFC 3986 allows
# `,` and `;` in a userinfo, so `http://me:pa,ss@proxy` and `http://me:s3cr;t@proxy` are real
# credentials — and the same bytes are what separates entries in a list, where they are not
# (`{"proxy":"http://h:3128","email":"a@b.c"}`, `NO_PROXY=http://h:80,user@x.example.test`). No
# pattern can tell the two apart from the text, and of the two mistakes a refusal is visible and
# names its reason while a leak is neither, so both stay **in** the password.
#
# `?`, `#`, and `"` stay *out*, and that is what keeps the false positives away rather than luck: a
# URL's authority ends at the first `?` or `#`, so no unencoded password can contain one, and a `"`
# (written `\"` in JSON, which the escape branch of the class puts back into the password)
# is what ends a JSON string — without it, `{"proxy":"http://h:3128","email":"a@b.c"}` matched from
# the port to the later `@`. A space or a `/` ends the match for the same reason.
#
# Issue #25 is where a user-defined allowlist would let a legitimate list through again.
_URL_CREDENTIALS = (
    rb"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,63}:(?:\\?/){2}"
    rb"[^/@\s:]{0,255}:%s(?:[^/@\s?#\"\\\\]|\\.){1,255}@"
)

# A URL the shell passes through verbatim, so its password is a literal and may hold anything a URL
# may — `,`, `;`, `?`, `#`, and `"` included. `'` stays out: it is what ends the quoted string, and
# `m='http://h:3128';me@x.com` would otherwise read `3128';me` as the password.
_URL_QUOTED_CREDENTIALS = rb"[A-Za-z][A-Za-z0-9+.\-]{0,63}:(?:\\?/){2}[^/@\s:]{0,255}:[^/@\s']{1,255}@"

# A password that is only a reference, in a file where the shell would expand it.
_REFERENCE_PASSWORD = rb"(?!\$\{?[A-Za-z_]\w*\}?@)"

# Only `text` is read by a shell, and only there does a `$` in a URL mean a reference rather than a
# character of a value. Every other format — including one this build has never heard of — gets the
# strict table, which is the fail-closed direction: a literal credential is refused, not skipped.
_SHELL_FORMATS = ("text",)

# A scheme and a token — `Basic`/`Bearer`/`Token` and what follows. The token characters are base64
# and base64url (`+`/`/`, `-`/`_`) plus `.`/`~`, so a JWT or a Google OAuth token is one run rather
# than stopping at its first dot. The width is **exact** (`{16}`), not a floor (`{16,}`): a floor lets
# the engine re-scan the rest of the value from every position, which is quadratic on a long line.
# Nothing is lost by that, because the allowlist's fingerprint is the whole line (`_lines_at`), not
# the matched token.
_TOKEN_CHARS = rb"A-Za-z0-9+/._~-"
_SCHEME_TOKEN = rb"[" + _TOKEN_CHARS + rb"]{16}={0,2}"

# ... and the token has to look encoded rather than be a word: a digit among its first 32 characters,
# or `=` padding right after it. Both tests are bounded, and the "both letter cases" test is gone —
# it is what refused a CamelCase word such as `ConfigurationOverridesForEditor`.
_SCHEME_SHAPE = rb"(?:(?=[" + _TOKEN_CHARS + rb"]{0,31}[0-9])|(?=[" + _TOKEN_CHARS + rb"]{16,32}={1,2}))"
# The gap between a scheme and its token: literal whitespace, or the JSON escape of one inside a
# value (`"Bearer\tTOKEN"`, which is what a structured file actually holds).
_GAP = rb"(?:[ \t]|\\[tnr])+"
_SCHEME_AND_TOKEN = rb"(?=[" + _TOKEN_CHARS + rb"]{16})" + _SCHEME_SHAPE + _SCHEME_TOKEN

# A word that may be a scheme this build has never heard of — `X-Custom`, `Token`, `AWS4-HMAC-SHA256`.
_SCHEME_WORD = rb"[A-Za-z][A-Za-z0-9+.\-]*"
_KNOWN_SCHEME = rb"(?i:basic|bearer|token|digest|negotiate|oauth|aws4-hmac-sha256)"

# A header key whose value may be a credential: `Authorization` and `Proxy-Authorization`. Only the
# key name is case-insensitive (`(?i:…)`), never the whole pattern: a global `re.IGNORECASE` also
# makes a `[A-Z]`/`[a-z]` test match any letter, so every word would look encoded.
_AUTHORIZATION_KEY = rb"\b(?i:(?:proxy-)?authorization)\b"

# The key-to-value join every settings format uses: `"Authorization": "…"` (JSONC), `Authorization:
# …` (YAML, shell, a `curl -H`), `Authorization = "…"` (TOML), and the escaped spellings a header
# carried inside a JSON string takes (`\"Authorization\": \"…\"`). The optional backslashes are that
# escaping; the optional quotes are the JSONC/JSON pair around the key and around the value. The
# whitespace runs are **possessive** (`*+`), so a long run of spaces cannot be backtracked over.
_AUTHORIZATION_SEPARATOR = rb"\\?[\"']?[ \t]*+[:=][ \t]*+\\?[\"']?"

# A value that is itself a URL is not a header credential (a *credentialed* URL is the URL rule's job).
_NOT_A_URL = rb"(?![ \t]*[A-Za-z][A-Za-z0-9+.\-]*://)"

# A value token that is *not* a reference, a command substitution, a placeholder, or a JSON literal:
# those mean "the real value lives elsewhere" or "nothing is set", so none is a credential. Angle
# brackets are deliberately **not** here: `Bearer {{x}} <token>` must not slip through just because
# the token sits behind a placeholder.
_HARMLESS_TOKEN = (
    # Every filler is **bounded**: an unclosed `${` or `{{` would otherwise be scanned to the end of
    # the input from every position, which is the same quadratic shape the token widths had.
    rb"(?:\$\{[^}]{0,256}\}|\$\([^)]{0,256}\)|`[^`]{0,256}`|\{\{[^}]{0,256}\}\}"
    rb"|\$\{?[A-Za-z_]\w*\}?|null|true|false|-?\d+(?:\.\d+)?)"
)
_CREDENTIAL_TOKEN = rb"(?!" + _HARMLESS_TOKEN + rb"(?:[\s\"',;}]|$))[^\s\"',;}]+"

# A **strong** token: 16+ base64-ish characters with room for `=` padding. Bounded, so an unknown
# scheme (`X-Custom abcdef0123456789`) is caught without the engine scanning to the end of the line.
_STRONG_TOKEN = rb"[" + _TOKEN_CHARS + rb"]{16,64}={0,2}"

# What is credential-shaped inside an `Authorization` value:
#  * a **known scheme** followed by harmless forms and then any token that is not one — a placeholder
#    does not hide the token behind it (`Bearer {{x}} <token>` is refused) — which is where the
#    lower-case `basic`, a short token, and `Bearer` land; or
#  * **any scheme word** followed by harmless forms and a **strong** token, so a scheme this build has
#    never heard of is caught when its credential is long enough to be one.
# A value with no space at all (`oauth2-client-credentials`) is neither, and neither is a bare word.
#
# The fingerprint is over the whole **line range** the match spans (`_lines_at`), so a match that ends
# early — `OAuth oauth_consumer_key=` before the `,` — or that runs across lines (a plist) still
# yields a fingerprint specific to those lines, and allowing it cannot pass a different header.
_AUTHORIZATION_CREDENTIAL = (
    rb"(?:"
    + _KNOWN_SCHEME
    + rb"(?:"
    + _GAP
    + _HARMLESS_TOKEN
    + rb")*"
    + _GAP
    + _CREDENTIAL_TOKEN
    + rb"|"
    + _SCHEME_WORD
    + rb"(?:"
    + _GAP
    + _HARMLESS_TOKEN
    + rb")*"
    + _GAP
    + _STRONG_TOKEN
    + rb")"
)

# Structured: the value (up to a closing quote, `,`, `;`, `}`, or a newline) must **begin** with a
# credential shape.
_STRUCTURED_AUTHORIZATION = re.compile(
    _AUTHORIZATION_KEY + _AUTHORIZATION_SEPARATOR + _NOT_A_URL + rb"(?=" + _AUTHORIZATION_CREDENTIAL + rb")[^\"',;}\n]+"
)

# Shell, double-quoted or unquoted: the value runs to a quote or the end of the line — **not** past a
# closing quote, so `curl -H "Authorization: Bearer $TOKEN" https://…` reads only the header. A
# reference is not credential-shaped, so the recommended shell spelling passes.
_SHELL_AUTHORIZATION = re.compile(
    _AUTHORIZATION_KEY + _AUTHORIZATION_SEPARATOR + _NOT_A_URL + rb"(?=" + _AUTHORIZATION_CREDENTIAL + rb")[^\"\n]+"
)


# A plist / XML header: `<key>Authorization</key>` followed by a `<string>` value. The `:`-form
# separator cannot see across `</key><string>`, so this is its own shape.
_AUTHORIZATION_XML = re.compile(
    rb"<\s*(?i:key)\s*>\s*"
    + _AUTHORIZATION_KEY
    + rb"\s*<\s*/\s*(?i:key)\s*>\s*<\s*(?i:string)\s*>\s*"
    + _AUTHORIZATION_CREDENTIAL
)

# The label lives with the allowlist's policy (`scan_allow`), so the label and the safety judgement
# cannot drift apart; it is imported above and re-exported through `__all__`.

# A structured file: any password, `$` and `{` included.
STRUCTURED_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    *COMMON_PATTERNS,
    (URL_CREDENTIALS_LABEL, re.compile(_URL_CREDENTIALS % rb"")),
    # An `Authorization` header value — `Basic <base64>` / `Bearer <token>` — which is how a
    # credential is spelled when it is **not** under a key a preset excludes: commented out, or
    # under a key this build has never heard of. Excluding a key is a promise about the key, and the
    # scan is what keeps the promise when the value is somewhere else in the same file (a VS Code
    # settings comment, for one).
    #
    # `[ \t]+`, not `\s+`: a whitespace class crosses a newline, and `// Visual Basic` above a banner
    # of slashes then reads `Basic` and the banner as one token. The token still has to *look*
    # encoded, because a false positive here stops **every** tracked file on the machine
    # (``AGENTS.md`` §6): `// Basic internationalization settings` and
    # `// Bearer settings/preferences/overrides` are prose, and stay prose.
    (BASIC_LABEL, re.compile(rb"\b(?i:basic)[ \t]+" + _SCHEME_AND_TOKEN)),
    (BEARER_LABEL, re.compile(rb"\b(?i:bearer)[ \t]+" + _SCHEME_AND_TOKEN)),
    # And the stronger form: the key itself names a credential, so a credential-shaped value under it
    # is refused whatever the scheme and length (#28).
    (AUTHORIZATION_LABEL, _STRUCTURED_AUTHORIZATION),
    # A plist / XML header, where the `:`-form separator cannot see across `</key><string>`.
    (AUTHORIZATION_LABEL, _AUTHORIZATION_XML),
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
        URL_CREDENTIALS_LABEL,
        re.compile(rb"(?<![A-Za-z0-9+.\-\\'])" + _URL_CREDENTIALS % _REFERENCE_PASSWORD),
    ),
    (URL_CREDENTIALS_LABEL, re.compile(rb"(?<=[\\'])" + _URL_QUOTED_CREDENTIALS)),
    # A `Bearer <token>` in a dotfile (an exported header, a `curl -H` that the Authorization rule
    # does not see because there is no key). The shape test keeps prose out.
    (BASIC_LABEL, re.compile(rb"\b(?i:basic)[ \t]+" + _SCHEME_AND_TOKEN)),
    (BEARER_LABEL, re.compile(rb"\b(?i:bearer)[ \t]+" + _SCHEME_AND_TOKEN)),
    # A header a shell passes to `curl -H` or writes into a dotfile.
    (AUTHORIZATION_LABEL, _SHELL_AUTHORIZATION),
)


def patterns_for(format: str) -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    """The pattern table for a file's format: the shell one only where a shell reads the text."""
    return SHELL_PATTERNS if format in _SHELL_FORMATS else STRUCTURED_PATTERNS


def _line_number(plaintext: bytes, offset: int) -> int:
    """The 1-based line a byte offset falls on."""
    return plaintext.count(b"\n", 0, offset) + 1


def _lines_at(plaintext: bytes, start: int, end: int) -> bytes:
    """The bytes of every line the span `[start, end)` touches, without the final newline.

    The allowlist's fingerprint is taken over the **lines a match spans**, not over the matched span.
    A pattern can stop before the value ends — `Authorization: OAuth oauth_consumer_key=` stops at the
    `,`, and a `Basic`/`Bearer` token can stop at a character the character class excludes — and two
    different credentials that share that prefix would then share a fingerprint, so allowing one would
    pass the other. A pattern can also *run across* lines (a plist `<key>…</key>` / `<string>…</string>`
    pair), and a fingerprint of only its first line would let every token under that key share it. The
    whole line range contains the whole value either way, so a fingerprint over it is specific to
    those lines.
    """
    line_start = plaintext.rfind(b"\n", 0, start) + 1
    last = max(start, end - 1)
    line_end = plaintext.find(b"\n", last)
    return plaintext[line_start:] if line_end == -1 else plaintext[line_start:line_end]


def _refusal(subject: str, label: str, line: int, digest: str | None) -> str:
    """The message a refusal shows: the location, never the matched bytes (``AGENTS.md`` §4).

    The fingerprint is included when the machine has a key, so the user can allow exactly the span
    they saw. It is keyed (``system/scan_allow.py``), so printing it reveals nothing about the
    value, and the suggested command copies straight back into `scan allow` — but only for a label
    that may be allowed at all: a pattern that matches fixed text (a PEM header) is not
    allowlistable, because one entry would pass every credential it stands in for.
    """
    message = f"{subject}: possible {label} at line {line}"
    if digest is None:
        return message
    if label not in ALLOWABLE_LABELS:
        return f"{message} (fingerprint {digest})"
    command = f"syncintosh scan allow {shlex.quote(subject)} {shlex.quote(label)} {digest}"
    return f"{message} (fingerprint {digest}; allow it with `{command}`)"


class PatternSecretScanner:
    """A `SecretScanner` over a fixed pattern table, minus this machine's allowed spans.

    `allow` is the per-machine allowlist (``system/scan_allow.py``). A match whose keyed
    fingerprint is on the list is skipped and the scan keeps looking; every other match is refused
    as before. With no allowlist — a build that has no key, or a unit test — a refusal simply omits
    the fingerprint.
    """

    def __init__(self, allow: ScanAllow | None = None) -> None:
        self._allow = allow

    def scan(self, plaintext: bytes, *, subject: str, format: str) -> None:
        """Raise `SecretScanError` naming the location when a pattern matches; never the value.

        `format` picks the URL table: a shell file may reference a credential, a structured file may
        not (see `patterns_for`). `finditer`, not `search`, so an allowed span early in the file
        does not hide a later, unallowed one. The fingerprint is over the whole **line range** a
        match spans (see `_lines_at`), so an allowlist entry is bound to those lines whatever span
        the pattern matched.
        """
        allow = self._allow
        for label, pattern in patterns_for(format):
            for match in pattern.finditer(plaintext):
                digest = (
                    None if allow is None else fingerprint(allow.key, _lines_at(plaintext, match.start(), match.end()))
                )
                if allow is not None and digest is not None and allow.allows(subject, label, digest):
                    continue
                line = _line_number(plaintext, match.start())
                raise SecretScanError(_refusal(subject, label, line, digest))


__all__ = ["COMMON_PATTERNS", "PatternSecretScanner", "patterns_for"]
