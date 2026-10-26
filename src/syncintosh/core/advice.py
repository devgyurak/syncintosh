# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a model may be shown and what a model may answer, as pure rules.

Assisted resolution sends a conflict to a model and reads one answer back. Both directions are
checked here, with no I/O, so the rules are testable with plain values (``AGENTS.md`` §2.1):

* **Out.** Every value leaves as a placeholder token. The masker (``system/masking.py``) builds
  the tokens; :func:`placeholder` and :func:`restore` define their shape, so the token format is
  decided in one place.
* **In.** An answer is strict JSON with exactly the keys ``choice``, ``value``, ``rationale``. A
  proposed value may use each placeholder once and no other token, or it is refused whole. A
  model cannot invent a value and smuggle it past the check by renaming a token.
* **Endpoint.** Whether a base URL is a loopback or private-network address, or a public one whose
  use needs the person's confirmation that data will leave the network (``AGENTS.md`` §3.5).

The model's text is data. Nothing here executes it or follows it (``AGENTS.md`` §3.5).
"""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urlsplit

# The answer a model gives for one conflict: take a side as it is, or propose a value.
AdviceChoice = Literal["local", "remote", "proposal"]

_CHOICES: Final[frozenset[str]] = frozenset({"local", "remote", "proposal"})
_KEYS: Final[frozenset[str]] = frozenset({"choice", "value", "rationale"})

# Every placeholder token starts with this prefix, so a token the model made up is easy to spot.
_PREFIX: Final = "<<snt:"
_TOKEN: Final = re.compile(r"<<snt:[0-9a-f]{8}:[0-9]+>>")

# A single character that a terminal or a reader could hide or reorder (see `visible`).
# The Unicode categories a reader cannot see: controls, format characters (zero-width, bidi marks and
# overrides), and the line and paragraph separators. A character in one of them can hide or reorder text.
_INVISIBLE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})

# The Tailscale and carrier-grade NAT range: private to the operator, not public, and not `is_private`.
_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")

# A whole terminal escape sequence, removed before its introducer character would be. Removing the
# characters alone would leave `[31m` behind as visible text.
_ESCAPE_SEQUENCE: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# A host as settings text writes it: a dotted name with a letter top level, a dotted IPv4 address, or the
# host of a URL or of a `user@host` form with any label count. The masker and the new-host check both read
# these patterns, so the two cannot disagree about what a host is.
HOST_TOKEN_PATTERN: Final = (
    r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![\w.-])"
    r"|(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])"
)
URL_HOST_PATTERN: Final = r"(?:(?<=://)|(?<=@))[A-Za-z0-9.-]+"
_HOST_TOKEN: Final = re.compile(HOST_TOKEN_PATTERN)
_URL_HOST: Final = re.compile(URL_HOST_PATTERN)

_NAME_CHAR: Final = re.compile(r"[A-Za-z0-9_.-]")
_UNICODE_ESCAPE: Final = re.compile(r"\\u([0-9a-fA-F]{4})")

# An all-numeric name as inet_aton reads one dotted part: `0x` hex, a leading `0` octal, or decimal.
_NUMERIC_PART: Final = re.compile(r"0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*")

# Hostnames a person runs on their own network. A name under one of these is internal even when
# the config does not list it.
_INTERNAL_SUFFIXES: Final = (".local", ".internal", ".lan", ".corp", ".home.arpa", ".localhost")


@dataclass(frozen=True)
class MaskedText:
    """One text with its values replaced by placeholder tokens.

    `values` maps each token to the original value it stands for. It is the plaintext the masking
    removed from the text, so it is excluded from `repr`: a log or a traceback never prints it.
    """

    text: str
    values: tuple[tuple[str, str], ...] = field(repr=False)


@dataclass(frozen=True)
class Advice:
    """A model's answer for one conflict. `value` is still masked when `choice` is a proposal."""

    choice: AdviceChoice
    value: str | None = field(default=None, repr=False)
    rationale: str = ""


def placeholder(nonce: str, index: int) -> str:
    """The token for the `index`-th value masked under `nonce`. One nonce covers one masking pass."""
    return f"<<snt:{nonce}:{index}>>"


def restore(proposed: str, values: Iterable[tuple[str, str]]) -> str | None:
    """The proposed value with every token put back, or `None` when a token is invented or repeated.

    The four texts of one conflict share one token table, and a correct merge uses only some of those
    tokens: the base's tokens are gone once the merge takes the remote's line. So a token the table
    does not hold, or one that appears twice, refuses the proposal. A token the proposal leaves out is
    allowed, and the person sees the result in the approval screen before anything is published.
    """
    table = dict(values)
    found = _TOKEN.findall(proposed)
    # A marker that starts like a token but is not one (a changed index or nonce) is refused too.
    stray = proposed.count(_PREFIX) != len(found)
    if stray or any(token not in table for token in found) or len(found) != len(set(found)):
        return None
    return _substitute(proposed, table)


def sanitize_rationale(text: str) -> str:
    """The rationale with its terminal escape sequences and invisible characters removed.

    A tab and a newline stay: a rationale may span lines. Every other character in `_INVISIBLE_CATEGORIES`
    goes, so a zero-width space or a bidi mark cannot hide text in what the person reads.
    """
    stripped = _ESCAPE_SEQUENCE.sub("", text)
    return "".join(char for char in stripped if char in "\t\n" or not _invisible(char))


def rationale_for_display(text: str, values: Iterable[tuple[str, str]]) -> str:
    """The rationale with the tokens it names put back. An unknown token stays as it was written."""
    return _substitute(sanitize_rationale(text), dict(values))


def parse_advice(raw: str) -> Advice | None:
    """A model's answer as an :class:`Advice`, or `None` when it is not the strict shape.

    The shape is one JSON object with exactly the keys `choice`, `value` and `rationale`. `value`
    is a string for a proposal and `null` for a side. Anything else is refused, not repaired.
    """
    try:
        payload = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        # ValueError covers a malformed document and a duplicate key; RecursionError a deeply nested one.
        return None
    if not isinstance(payload, dict) or set(payload) != _KEYS:
        return None
    choice, value, rationale = payload["choice"], payload["value"], payload["rationale"]
    if not isinstance(choice, str) or choice not in _CHOICES or not isinstance(rationale, str):
        return None
    if choice == "proposal":
        if not isinstance(value, str):
            return None
        return Advice(choice="proposal", value=value, rationale=sanitize_rationale(rationale))
    if value is not None:
        return None
    return Advice(choice="local" if choice == "local" else "remote", rationale=sanitize_rationale(rationale))


def is_internal_host(host: str, configured: Iterable[str] = ()) -> bool:
    """True when `host` names a loopback or private-network address, or a name in such a domain.

    A name is internal when it is `localhost`, sits under a built-in internal suffix, or sits under
    one the config lists. An IP literal is internal when it is loopback, link-local, or private
    (RFC 1918 and its IPv6 equivalents).
    """
    name = host.strip("[]").rstrip(".").lower()
    if name == "":
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        # Not a dotted or colon-separated literal: an all-numeric name still denotes an address, as the
        # resolver reads it (`134744072` is 8.8.8.8).
        address = numeric_host(name)
    if address is not None:
        return address.is_loopback or address.is_link_local or address.is_private or address in _CGNAT
    if name == "localhost" or name == "home.arpa" or name.endswith(_INTERNAL_SUFFIXES):
        return True
    if "." not in name:
        # A single-label name only resolves on the local network (a Bonjour or DHCP name like `mac-mini`).
        return True
    return any(name == domain or name.endswith(f".{domain}") for domain in _normalized(configured))


def is_public_endpoint(base_url: str, configured: Iterable[str] = ()) -> bool:
    """True when `base_url` does not point at a loopback or private-network host.

    A public endpoint sends the conflict off the network, so the person confirms it before the
    first call. `configured` are the config's `internal_domains`. A URL with no host is public: an
    unclear endpoint needs confirming.
    """
    host = urlsplit(base_url).hostname or ""
    return not is_internal_host(host, configured)


def visible(text: str) -> str:
    """The text with every character of category Cc, Cf, Zl or Zp written as a `\\uXXXX` escape.

    What a person approves must be what they read. A raw carriage return, escape sequence, zero-width
    space, bidi mark, or line separator in a proposal would show one thing and apply another.
    """
    return "".join(f"\\u{ord(char):04x}" if _invisible(char) else char for char in text)


def numeric_host(name: str) -> ipaddress.IPv4Address | None:
    """The IPv4 address an all-numeric name denotes under `inet_aton`'s rules, or `None` for any other name.

    The resolver reads `134744072` and `0x08080808` as 8.8.8.8, and `127.1` as 127.0.0.1. Judging the
    spelling alone would call such a name a local name, and a public address would skip the plain-http
    refusal and the confirmation.
    """
    parts = name.split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMERIC_PART.fullmatch(part) for part in parts):
        return None
    *leading, last = [_numeric_value(part) for part in parts]
    if any(value > 255 for value in leading) or last >= 256 ** (4 - len(leading)):
        return None
    address = last
    for index, value in enumerate(leading):
        address |= value << (8 * (3 - index))
    return ipaddress.IPv4Address(address)


def host_tokens(text: str) -> frozenset[str]:
    """Every host the text names, lowercased: a dotted name, a dotted address, or a URL or `user@` host.

    The text is read as written and with its JSON escapes undone, since a proposal can escape its slashes.
    A dotted name that is a path segment (`y.git` in `x/y.git`) or a settings key (`"editor.fontSize":`) is
    not a destination, so it is not counted. The masker does not use this filter: it hides such names too.
    """
    found: set[str] = set()
    for form in _unescaped_forms(text):
        found |= {
            match.group().lower()
            for match in _HOST_TOKEN.finditer(form)
            if not _is_escape_residue(form, match.start())
            and not _is_path_segment(form, match.start())
            and not _is_setting_key(form, match.end())
        }
        found |= {match.group().lower() for match in _URL_HOST.finditer(form)}
    return frozenset(found)


def _unescaped_forms(text: str) -> tuple[str, ...]:
    """The text as written, with `\\/` undone, with `\\uXXXX` decoded, and with both."""
    unslashed = text.replace("\\/", "/")
    decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    both = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), unslashed)
    return tuple(dict.fromkeys((text, unslashed, decoded, both)))


def new_host_tokens(proposal: str, *sides: str) -> tuple[str, ...]:
    """The hosts a proposal names that none of the sides named, sorted.

    A host the proposal introduces can receive data the person did not agree to: a `git@host:` remote, a
    proxy, a `curl ftp://host` pipe. So the check is on any host token, not only on a full URL.
    """
    known: frozenset[str] = frozenset().union(*(host_tokens(side) for side in sides))
    return tuple(sorted(host_tokens(proposal) - known))


def _numeric_value(part: str) -> int:
    if part[:2] in ("0x", "0X"):
        return int(part[2:] or "0", 16)
    if len(part) > 1 and part.startswith("0"):
        return int(part, 8)
    return int(part)


def _is_path_segment(text: str, start: int) -> bool:
    """True when the name follows a `/` that a path component uses, so `x/y.git` but not `http:/host` or `\\/host`.

    The character before the slash must be a name character. After `:`, `\\`, or another `/` the slash
    is a scheme or an escape, and the name after it is a host.
    """
    return start >= 2 and text[start - 1] == "/" and _NAME_CHAR.match(text[start - 2]) is not None


def _is_escape_residue(text: str, start: int) -> bool:
    """True when the name follows a backslash, so it is the tail of a `\\uXXXX` escape (`u002fevil.example`).

    The escape-decoded form already holds the real host, so the residue is not counted.
    """
    return start >= 1 and text[start - 1] == "\\"


def _is_setting_key(text: str, end: int) -> bool:
    """True when the name is followed by its closing quote and a colon, as a JSON key is.

    A colon directly followed by a digit (`'evil.example':8080`) is a port, not a key. A key with a space and
    a value (`"editor.fontSize": 13`) is still excluded. The compact `"key":13` form is counted as a host,
    which is the safe side.
    """
    return _KEY_END.match(text, end) is not None


_KEY_END: Final = re.compile(r"[\"']\s*:(?!\d)")


def _invisible(char: str) -> bool:
    return unicodedata.category(char) in _INVISIBLE_CATEGORIES


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build the object, refusing a repeated key: `json` would otherwise keep only the last one."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key in the answer")
    return dict(pairs)


def _normalized(domains: Iterable[str]) -> tuple[str, ...]:
    return tuple(domain.strip(".").lower() for domain in domains if domain.strip(".") != "")


def _substitute(text: str, table: Mapping[str, str]) -> str:
    return _TOKEN.sub(lambda match: table.get(match.group(0), match.group(0)), text)


__all__ = [
    "HOST_TOKEN_PATTERN",
    "URL_HOST_PATTERN",
    "Advice",
    "AdviceChoice",
    "MaskedText",
    "host_tokens",
    "is_internal_host",
    "is_public_endpoint",
    "new_host_tokens",
    "numeric_host",
    "parse_advice",
    "placeholder",
    "rationale_for_display",
    "restore",
    "sanitize_rationale",
    "visible",
]
