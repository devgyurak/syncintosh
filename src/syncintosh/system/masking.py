# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `Masker` that hides secrets, internal hosts, the home path, and the username from a model.

The secret patterns are the scanner's own (``system/secret_scan.py``): the structured table, the shell
table, and the candidates a check confirms, so a credential the scanner would refuse under either format
is masked here.
Matching runs on bytes, the way the scanner runs, and placeholder tokens are ASCII, so the masked
text decodes exactly as the original did.

Each occurrence gets its own token, under one nonce per call. A proposal must name each token
once, so a value that appears twice in a conflict is two tokens the proposal must both carry
(``core/advice.py``).
"""

from __future__ import annotations

import re
import secrets
from typing import Final

from syncintosh.core.advice import (
    HOST_TOKEN_PATTERN,
    URL_HOST_PATTERN,
    MaskedText,
    is_internal_host,
    placeholder,
    restore,
)
from syncintosh.system.secret_scan import (
    CHECKED_PATTERNS,
    SHELL_PATTERNS,
    STRUCTURED_PATTERNS,
    yaml_block_authorizations,
)

# Both tables, so masking is at least as broad as either scan. Duplicates only cost a second pass.
_SECRET_PATTERNS: Final = (*STRUCTURED_PATTERNS, *SHELL_PATTERNS)

# What follows a capped credential token: the rest of the base64 run, empty-matching at worst.
_B64_RUN: Final = re.compile(rb"[A-Za-z0-9+/=]*")

# The host patterns are the advice module's, so masking and the new-host check read one definition.
_HOST: Final = re.compile(HOST_TOKEN_PATTERN.encode("ascii"))
_URL_HOST: Final = re.compile(URL_HOST_PATTERN.encode("ascii"))

# A value written under a credential-like key, in JSON, YAML, or shell. The value is one quoted string,
# with its escapes, or the unquoted run up to the end of the line or a delimiter. The separator may span a
# line break, so `"github.token":` followed by the value on the next line is still one pair. Not exhaustive:
# a value under another key name is still sent, and the design note says so.
_KEYED_VALUE: Final = re.compile(
    rb"(?i:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|private[_-]?key"
    rb"|auth|authorization|bearer|cookie|session|pass)"
    # Up to four more name parts (`secret_access_key`, `SECRET_ACCESS_KEY`, `secretAccessKey`). Outside the
    # `(?i)` on purpose: a camelCase part has to start with a capital, so `authorName` and `tokenizer` are
    # not a keyword plus a part. `session_timeout` is masked too; a harmless over-mask beats a leaked key.
    rb"(?:[_-][A-Za-z0-9]+|[A-Z][a-z0-9]*){0,4}"
    rb"[\"']?\s*[:=]\s*"
    # An unquoted value cannot start with `{` or `[`: that is an object or array under the key (`"tokenColors": {`),
    # whose structure the model needs, and whose members are masked on their own keys.
    rb"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s{\[\r\n\"',;}\]][^\r\n\"',;}\]]*)"
)


class TextMasker:
    """Masks one conflict's texts together, so one nonce and one value table cover them all.

    The constructor takes the values it masks and does no I/O. The nonce is drawn per `mask` call,
    so tokens from one call cannot be replayed into another.
    """

    def __init__(self, *, home: str, username: str, internal_domains: tuple[str, ...] = ()) -> None:
        self._home = home
        self._username = username
        self._internal_domains = internal_domains

    def mask(self, *texts: str) -> tuple[MaskedText, ...]:
        nonce = secrets.token_hex(4)
        table: list[tuple[str, str]] = []
        rendered: list[str] = []
        for text in texts:
            data = text.encode("utf-8")
            out = bytearray()
            cursor = 0
            for start, end in self._spans(data):
                token = placeholder(nonce, len(table))
                table.append((token, data[start:end].decode("utf-8")))
                out += data[cursor:start] + token.encode("ascii")
                cursor = end
            out += data[cursor:]
            rendered.append(out.decode("utf-8"))
        values = tuple(table)
        return tuple(MaskedText(text=text, values=values) for text in rendered)

    def unmask(self, proposed: str, masked: MaskedText) -> str | None:
        return restore(proposed, masked.values)

    def _spans(self, data: bytes) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for _, pattern in _SECRET_PATTERNS:
            found.extend(_secret_span(match) for match in pattern.finditer(data))
        for _, pattern, check in CHECKED_PATTERNS:
            # The pattern stops at its width cap; the rest of the run is still the credential's tail.
            found.extend(
                (match.start(), _run_end(data, match.end())) for match in pattern.finditer(data) if check(match)
            )
        # A YAML block scalar's continuation lines: `_KEYED_VALUE` only hides the `>-` after the key. Run on
        # every text, whatever its format, because the masker must cover at least what the scan refuses.
        found.extend((start, end) for _, start, end in yaml_block_authorizations(data) if start < end)
        found.extend(self._literal_spans(data))
        found.extend(self._host_spans(data))
        return _non_overlapping(found)

    def _literal_spans(self, data: bytes) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        if self._home != "":
            spans.extend(match.span() for match in re.finditer(re.escape(self._home.encode("utf-8")), data))
        if self._username != "":
            # Whole words only, so `al` does not hide the `al` inside `false`. The boundary is an ASCII
            # class, because `\b` is ASCII-only in a bytes pattern and a non-ASCII name would never match.
            name = re.escape(self._username.encode("utf-8"))
            spans.extend(
                match.span() for match in re.finditer(rb"(?<![A-Za-z0-9_])" + name + rb"(?![A-Za-z0-9_])", data)
            )
        return spans

    def _host_spans(self, data: bytes) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for match in _HOST.finditer(data):
            if is_internal_host(match.group().decode("ascii"), self._internal_domains):
                spans.append(match.span())
        for match in _URL_HOST.finditer(data):
            if is_internal_host(match.group().decode("ascii"), self._internal_domains):
                spans.append(match.span())
        for match in _KEYED_VALUE.finditer(data):
            spans.append(match.span(1))
        return spans


def _secret_span(match: re.Match[bytes]) -> tuple[int, int]:
    """The span to hide: the pattern's `secret` group when it has one, else the whole match.

    A rule anchored on a key name (`AWS_SECRET_ACCESS_KEY=`) marks only the value, so the model still
    sees which setting it is merging.
    """
    return match.span("secret") if "secret" in match.re.groupindex else match.span()


def _run_end(data: bytes, end: int) -> int:
    """Where the base64 run that starts at `end` stops (`end` itself when none follows)."""
    tail = _B64_RUN.match(data, end)
    return end if tail is None else tail.end()


def _non_overlapping(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The spans with every overlap merged into one, so no fragment of a matched value is left out.

    Two overlapping matches are one value that two rules both claim (a home path that is also the
    start of an internal host). Dropping the later one would leave its tail in the text.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


__all__ = ["TextMasker"]
