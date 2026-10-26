# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `Masker` that hides secrets, internal hosts, the home path, and the username from a model.

The secret patterns are the scanner's own, both tables (``system/secret_scan.py``): the structured
one and the shell one, so a credential the scanner would refuse under either format is masked here.
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
from syncintosh.system.secret_scan import SHELL_PATTERNS, STRUCTURED_PATTERNS

# Both tables, so masking is at least as broad as either scan. Duplicates only cost a second pass.
_SECRET_PATTERNS: Final = (*STRUCTURED_PATTERNS, *SHELL_PATTERNS)

# The host patterns are the advice module's, so masking and the new-host check read one definition.
_HOST: Final = re.compile(HOST_TOKEN_PATTERN.encode("ascii"))
_URL_HOST: Final = re.compile(URL_HOST_PATTERN.encode("ascii"))

# A value written under a credential-like key, in JSON, YAML, or shell. The value is one quoted string,
# with its escapes, or the unquoted run up to the end of the line or a delimiter. The separator may span a
# line break, so `"github.token":` followed by the value on the next line is still one pair. Not exhaustive:
# a value under another key name is still sent, and the design note says so.
_KEYED_VALUE: Final = re.compile(
    rb"(?i:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|private[_-]?key"
    rb"|auth|authorization|bearer|cookie|session|pass)[\"']?\s*[:=]\s*"
    rb"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n\"',;}\]]+)"
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
            found.extend(match.span() for match in pattern.finditer(data))
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
