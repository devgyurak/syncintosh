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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urlsplit

# The answer a model gives for one conflict: take a side as it is, or propose a value.
AdviceChoice = Literal["local", "remote", "proposal"]

_CHOICES: Final[frozenset[str]] = frozenset({"local", "remote", "proposal"})
_KEYS: Final[frozenset[str]] = frozenset({"choice", "value", "rationale"})

# Every placeholder token starts with this prefix, so a token the model made up is easy to spot.
_TOKEN: Final = re.compile(r"<<snt:[0-9a-f]{8}:[0-9]+>>")

# Terminal escape sequences, C0 and C1 control characters, and bidirectional overrides: each one a
# model could use to hide text, repaint the screen, or reorder what the person reads. Tab and newline
# stay, because a rationale is allowed to span lines.
_CONTROL: Final = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)

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
    """The proposed value with every token put back, or `None` when the tokens do not match exactly.

    A proposal settles only when it names each token of the masked conflict exactly once and no
    other token, so a missing, duplicated, or invented token refuses the whole proposal.
    """
    table = dict(values)
    found = _TOKEN.findall(proposed)
    if sorted(found) != sorted(table):
        return None
    return _substitute(proposed, table)


def sanitize_rationale(text: str) -> str:
    """The rationale with its terminal escapes and control characters removed."""
    return _CONTROL.sub("", text)


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
        address = None
    if address is not None:
        return address.is_loopback or address.is_link_local or address.is_private
    if name == "localhost" or name == "home.arpa" or name.endswith(_INTERNAL_SUFFIXES):
        return True
    return any(name == domain or name.endswith(f".{domain}") for domain in _normalized(configured))


def is_public_endpoint(base_url: str) -> bool:
    """True when `base_url` does not point at a loopback or private-network host.

    A public endpoint sends the conflict off the network, so the person confirms it before the
    first call. A URL with no host is treated as public: an unclear endpoint needs confirming.
    """
    host = urlsplit(base_url).hostname or ""
    return not is_internal_host(host)


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
    "Advice",
    "AdviceChoice",
    "MaskedText",
    "is_internal_host",
    "is_public_endpoint",
    "parse_advice",
    "placeholder",
    "rationale_for_display",
    "restore",
    "sanitize_rationale",
]
