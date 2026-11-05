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

import base64
import binascii
import codecs
import re
import shlex
from collections.abc import Callable
from typing import NamedTuple

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
    # The group is **atomic** (`(?>`): `${A}` fits both `\$\{…\}` and `\$\{?\w+\}?`, and in the
    # `(?:GAP HARMLESS)*` repeat that split doubled the work per token (`${A}` x 26 ran for 20 s).
    rb"(?>\$\{[^}]{0,256}\}|\$\([^)]{0,256}\)|`[^`]{0,256}`|\{\{[^}]{0,256}\}\}"
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


# A `Basic` credential is decoded, not shape-guessed: the scheme is defined as `base64(user ":" password)`
# (RFC 7617), so a token that decodes to printable text with a `user:` in front is a credential whatever
# its digits and padding look like. `_SCHEME_SHAPE` only sees a digit or `=`, and misses a 16-character
# token that has neither. The width is capped (and the run possessive) so the engine cannot re-scan a
# long value from every position; a run past the cap is cut there and `_is_basic_credential` decodes the
# prefix, which already holds the `user:` that decides it. The standard alphabet only: Basic is not
# base64url.
_BASIC_WIDTH_CAP = 256
_BASIC_DECODED = re.compile(rb"\b(?i:basic)[ \t]+(?P<token>[A-Za-z0-9+/]{16,256}+={0,2}+)")


# C0 controls except tab, DEL, and C1 controls: what a password does not hold. `str.isprintable` is
# stricter than that and would reject a password with a tab or a no-break space.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def _is_basic_credential(match: re.Match[bytes]) -> bool:
    """True when the token strictly decodes to printable UTF-8 shaped `user:password` with a user."""
    token = match.group("token")
    # Only a run cut at the width cap is a prefix: it decodes by its whole quads, and `final=False` lets a
    # multibyte character the cut split in half through. Any other token is the whole value, so it is
    # padded to a whole number of quads and decoded in full (a length of 1 mod 4 is not base64 at all).
    # Trimming an uncut token instead would let a dictionary-shaped word decode by accident.
    capped = len(token) == _BASIC_WIDTH_CAP
    if capped:
        whole = token[: len(token) - len(token) % 4]
    elif token.endswith(b"="):
        whole = token
    elif len(token) % 4 == 1:
        return False
    else:
        whole = token + b"=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(whole, validate=True)
        decoded = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not capped)
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, separator, _ = decoded.partition(":")
    return separator == ":" and user != "" and _CONTROL.search(decoded) is None


# `(label, pattern, check)` — a pattern whose match is only a *candidate*, confirmed by `check`. Kept
# apart from the `(label, pattern)` tables so those keep their shape; the scanner and the masker both
# read this one, so the masker covers exactly what the scan refuses.
CHECKED_PATTERNS: tuple[tuple[str, re.Pattern[bytes], Callable[[re.Match[bytes]], bool]], ...] = (
    (BASIC_LABEL, _BASIC_DECODED, _is_basic_credential),
)


# A YAML value can sit on the lines *after* its key, where the same-line rules cannot see it:
#
# * a **block scalar** (`Authorization: >-` / `|`, with an optional anchor or tag before the indicator),
#   whose scheme and token may even sit on different lines;
# * a **plain or quoted scalar** that continues (`Authorization: Token` and then `abc123`), or starts on
#   the next line (`Authorization:` and then `Token abc123`).
#
# The key must open its line (after indentation, `- ` list markers, a quote, and a name prefix such as
# `X-` or `Proxy-`): inside a flow mapping or a longer string the same-line rules own it.
_YAML_KEY_LINE = re.compile(_AUTHORIZATION_KEY + rb"[\"']?[ \t]*+:(?P<rest>[^\n]*)")
_YAML_BOM = b"\xef\xbb\xbf"
_YAML_KEY_PREFIX = re.compile(rb"[ \t]*(?:-[ \t]+)*(?:[!&]\S*[ \t]+)*[\"']?[\w.\-]*")
_YAML_INDENT = re.compile(rb"[ \t]*(?:-[ \t]+)*")
_YAML_BLOCK_INDICATOR = re.compile(rb"[|>](?:[+-][1-9]?|[1-9][+-]?)?")
_YAML_PROPERTIES = re.compile(rb"(?:[!&]\S*(?:[ \t]+|$))*")
_YAML_COMMENT = re.compile(rb"(?:^|[ \t])#.*$")
_YAML_BLANK_RUN = re.compile(rb"(?:[ \t\r]*\n)*")
# What may open a plain or quoted value before its scheme: a quote, an anchor, or a tag.
_YAML_VALUE_LEAD = re.compile(rb"(?:[\"']|[!&]\S*[ \t]+)*")
_YAML_SEQUENCE_MARKER = re.compile(rb"(?:-(?:[ \t]+|$))+")
_YAML_CREDENTIAL = re.compile(_AUTHORIZATION_CREDENTIAL)

# Gathering a value is bounded, so a key followed by a very long indented run costs a constant per key
# rather than a scan to the end of the file. Blank lines are skipped in one step and count toward the byte
# cap only: YAML keeps leading blank lines in a block scalar, so a credential can sit after any number of
# them. A **block scalar** that a cap cut short is not judged on the lines read: it is refused (and masked)
# whole, to the end of its indented region, so what was not read cannot be called harmless. A plain or
# quoted value past the caps is read no further, which is the one place this stays a limit: nothing writes a
# header credential as a 64-line plain scalar, and refusing every large `authorization:` settings mapping
# would stop the machine's files for no gain.
_YAML_BLOCK_MAX_LINES = 64
_YAML_BLOCK_MAX_BYTES = 65536


def yaml_block_authorizations(plaintext: bytes) -> list[tuple[int, int, int]]:
    """Every credential-shaped `Authorization` value that continues past its key line in YAML.

    Each result is `(key_line_start, value_start, value_end)`: the scan fingerprints the lines from the key
    line to the last line of the value (`_lines_at`), and the masker hides `[value_start, value_end)`. The
    value is the rest of the key line plus the following lines indented deeper than the key (blank lines
    skipped, the first line that is not deeper ends it), joined with one space and tested with the ordinary
    `Authorization` credential rule, so `>` (folded), `|` (literal), and a continued plain scalar are read
    the same way. A key inside a value already reported is skipped, which keeps the walk linear. This lives
    here, with the other patterns, so that the scan and the masker share one definition (``AGENTS.md``
    §3.4).

    Not read, and so a limit: a key inside a flow mapping or behind a `?` explicit-key marker, a CR-only
    line ending, an escaped line break inside a double-quoted value, a key with a word glued to its
    front (`my_authorization`), which the same-line rules cannot see either, and a `-` line indented deeper
    than its list item (`- Bearer` / `    - abc`), which YAML reads as the item's text but is split here.
    A list item that is itself a block scalar (`- |` then `Token abc`) is not read either: the `|` in
    front keeps the item from looking like a credential.
    """
    found: list[tuple[int, int, int]] = []
    reported_until = 0
    for key in _YAML_KEY_LINE.finditer(plaintext):
        if key.start() < reported_until:
            continue
        line_start = plaintext.rfind(b"\n", 0, key.start()) + 1
        prefix = plaintext[line_start : key.start()].removeprefix(_YAML_BOM)
        if _YAML_KEY_PREFIX.fullmatch(prefix) is None:
            continue
        # The key's column: indentation and list markers only, so `X-Authorization:` and a quoted key are
        # measured where the mapping starts, and a sibling at the same column ends the value.
        span = _yaml_value(plaintext, key, _match_end(_YAML_INDENT, prefix, 0))
        if span is not None:
            found.append((line_start, *span))
            reported_until = span[1]
    return found


def _yaml_value(plaintext: bytes, key: re.Match[bytes], indent: int) -> tuple[int, int] | None:
    """The `(start, end)` of a credential-shaped value that continues past `key`'s line, else None."""
    rest = _YAML_COMMENT.sub(b"", key.group("rest").rstrip(b"\r")).rstrip()
    lead = len(rest) - len(rest.lstrip())
    value_offset = _match_end(_YAML_PROPERTIES, rest, lead)
    inline = rest[value_offset:]
    block = _YAML_BLOCK_INDICATOR.fullmatch(inline) is not None
    inline_start = key.start("rest") + value_offset
    walk = _gather_continuation(plaintext, key.end(), indent, block=block)
    if block and walk.region_end is not None:
        return (key.end() if walk.start == -1 else walk.start, walk.region_end)
    lines = ([] if block or inline == b"" else [inline]) + walk.parts
    # A same-line value is the same-line rules' job, except behind an anchor or a tag, which they cannot
    # read past (`Authorization: !!str Token abc`).
    if not lines or not (walk.parts or value_offset > lead):
        return None
    # A value whose first line carries a `-` is a sequence: a list of values, each judged alone, so `- Basic`
    # and `- Bearer` are scheme names rather than `Basic -` and a token. Any other value is one scalar, and
    # a later line that happens to start with `-` is just its text (`Token` / `- abc123`). Inline comments
    # are not content outside a block scalar.
    if not block:
        lines = [_YAML_COMMENT.sub(b"", line).strip() for line in lines]
    first = _YAML_SEQUENCE_MARKER.match(lines[0])
    items: list[bytes] = [b" ".join(lines)]
    if first is not None:
        items = []
        for line in lines:
            marker = _YAML_SEQUENCE_MARKER.match(line)
            if marker is not None or not items:
                items.append(line[0 if marker is None else marker.end() :])
            else:
                items[-1] += b" " + line
    if not any(
        _YAML_CREDENTIAL.match(i, _match_end(_YAML_VALUE_LEAD, i, 0)) is not None for i in map(bytes.strip, items)
    ):
        return None
    start = walk.start if inline == b"" or block else inline_start
    end = walk.end if walk.parts else inline_start + len(inline)
    return (start, end)


def _match_end(pattern: re.Pattern[bytes], data: bytes, pos: int, endpos: int | None = None) -> int:
    """Where `pattern` ends when it matches at `pos` (these patterns can match empty), else `pos`."""
    found = pattern.match(data, pos, len(data) if endpos is None else endpos)
    return pos if found is None else found.end()


class _Continuation(NamedTuple):
    """What follows a key line: the content read (`start`..`end`, -1 when none) and the lines' text.

    `region_end` is set only when a cap stopped the walk with more deeper-indented text ahead: it is where
    that text ends, so a caller that must not pass the unread part can cover all of it.
    """

    start: int
    end: int
    parts: list[bytes]
    region_end: int | None


def _gather_continuation(plaintext: bytes, header_end: int, indent: int, *, block: bool) -> _Continuation:
    """The non-blank lines after a key line that are indented deeper than `indent`.

    A plain or quoted value (not a `block`) also continues over a `- ` line at the key's own column, which
    is how ruamel and PyYAML dump a sequence, and skips comment lines. Inside a block scalar a `#` line is
    content and a line at the key's column ends it. `region_end` is computed for a block only: it is the
    one caller that reports the unread part, and walking it for a plain value is wasted work that a
    staircase of nested keys would turn quadratic.
    """
    limit = min(len(plaintext), header_end + _YAML_BLOCK_MAX_BYTES)
    parts: list[bytes] = []
    start = end = -1
    cursor = plaintext.find(b"\n", header_end)
    while cursor != -1:
        line_start = _match_end(_YAML_BLANK_RUN, plaintext, cursor + 1, limit)
        if line_start >= limit:
            if not block:
                break
            # Only the byte cap can stop here; look past it once, to see whether text is really ahead.
            line_start = _match_end(_YAML_BLANK_RUN, plaintext, line_start)
        if line_start >= len(plaintext):
            break
        line_end = plaintext.find(b"\n", line_start)
        line_end = len(plaintext) if line_end == -1 else line_end
        line = plaintext[line_start:line_end]
        text = line.lstrip(b" \t")
        column = len(line) - len(text)
        # A comment line is skipped at any column outside a block scalar, where it is content.
        comment = not block and text.startswith(b"#")
        sibling_item = not block and column == indent and (text.startswith(b"- ") or text.rstrip() == b"-")
        if column <= indent and text.strip() != b"" and not sibling_item and not comment:
            break
        if text.strip() != b"" and not comment:
            if len(parts) >= _YAML_BLOCK_MAX_LINES or line_start > limit:
                if not block:
                    break
                return _Continuation(start, end, parts, _region_end(plaintext, line_start, indent))
            start = line_start + len(line) - len(text) if start == -1 else start
            end = line_start + len(line.rstrip())
            parts.append(text.strip())
        cursor = line_end if line_end < len(plaintext) else -1
    return _Continuation(start, end, parts, None)


def _region_end(plaintext: bytes, position: int, indent: int) -> int:
    """Where the text indented deeper than `indent` ends, from the content line at `position` on."""
    end = position
    cursor = position
    while cursor < len(plaintext):
        cursor = _match_end(_YAML_BLANK_RUN, plaintext, cursor)
        line_end = plaintext.find(b"\n", cursor)
        line_end = len(plaintext) if line_end == -1 else line_end
        line = plaintext[cursor:line_end]
        if cursor >= len(plaintext) or len(line) - len(line.lstrip(b" \t")) <= indent:
            break
        end = cursor + len(line.rstrip())
        cursor = line_end + 1
    return end


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
        checked = ((label, pattern, lambda _match: True) for label, pattern in patterns_for(format))
        for label, pattern, check in (*checked, *CHECKED_PATTERNS):
            for match in pattern.finditer(plaintext):
                if not check(match):
                    continue
                self._refuse_unless_allowed(plaintext, match.start(), match.end(), label, subject)
        if format == "yaml":
            for key_line_start, _, value_end in yaml_block_authorizations(plaintext):
                self._refuse_unless_allowed(plaintext, key_line_start, value_end, AUTHORIZATION_LABEL, subject)

    def _refuse_unless_allowed(self, plaintext: bytes, start: int, end: int, label: str, subject: str) -> None:
        """Return when the lines `[start, end)` touch are allowed on this machine; raise otherwise."""
        allow = self._allow
        digest = None if allow is None else fingerprint(allow.key, _lines_at(plaintext, start, end))
        if allow is not None and digest is not None and allow.allows(subject, label, digest):
            return
        raise SecretScanError(_refusal(subject, label, _line_number(plaintext, start), digest))


__all__ = ["CHECKED_PATTERNS", "COMMON_PATTERNS", "PatternSecretScanner", "patterns_for", "yaml_block_authorizations"]
