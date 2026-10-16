# SPDX-License-Identifier: AGPL-3.0-or-later
"""`keys/commitments.json`: the cleartext map of generation id to key commitment.

A commitment is `HMAC-SHA256(KG_n, b"syncintosh/v1/commit")` (``crypto/commitments.py``). It
must be readable **without** the key — the manifest that would otherwise carry the list is
itself encrypted *under* the generation in question — and it leaks nothing: a PRF output over a
public string, keyed by 256 random bits (``docs/design/05-key-generations.md``).

This module owns the **shape and canonical serialization** of the file, because the no-rewrite
rule compares plaintext bytes: the map must serialize identically whatever order it was built
in. The keyed computation lives in `crypto`, behind the `RingCodec` port, so `core` stays free
of a key. Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from syncintosh.core.errors import CommitmentsCorrupt
from syncintosh.core.manifest_codec import canonical_json_bytes

# 64 lowercase hex characters — the hex of a SHA-256 MAC.
_COMMITMENT_RE = re.compile(r"[0-9a-f]{64}")
# A generation id is written as its decimal string, and generation 0 does not exist.
_GENERATION_RE = re.compile(r"[1-9][0-9]*")


def dumps_commitments(commitments: Mapping[int, str]) -> bytes:
    """Canonical bytes for a `{generation id: commitment}` map.

    Keys are the decimal string of the id and keys are sorted, so the same map always produces
    the same bytes (``docs/design/03-manifest.md``). A bad id or commitment is refused here, so
    the encoder cannot write a file the parser would reject.
    """
    payload: dict[str, str] = {}
    for key_id, commitment in commitments.items():
        if not isinstance(key_id, int) or isinstance(key_id, bool) or key_id < 1:
            raise CommitmentsCorrupt(f"generation id must be a positive integer, got {key_id!r}")
        if not isinstance(commitment, str) or _COMMITMENT_RE.fullmatch(commitment) is None:
            raise CommitmentsCorrupt(f"commitment for generation {key_id} is not 64 lowercase hex characters")
        payload[str(key_id)] = commitment
    return canonical_json_bytes(payload)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CommitmentsCorrupt(f"duplicate generation key {key!r} in commitments")
        result[key] = value
    return result


def _parse_entry(raw: object, key: str) -> tuple[int, str]:
    if _GENERATION_RE.fullmatch(key) is None:
        raise CommitmentsCorrupt(f"commitments key {key!r} is not a positive generation id")
    if not isinstance(raw, str) or _COMMITMENT_RE.fullmatch(raw) is None:
        raise CommitmentsCorrupt(f"commitment for generation {key} is not 64 lowercase hex characters")
    return int(key), raw


def loads_commitments(data: bytes) -> dict[int, str]:
    """Parse commitment bytes into `{generation id: commitment}`.

    Rejects a non-UTF-8 payload, malformed JSON, a duplicate generation, a non-positive id, and
    a value that is not a 64-lowercase-hex commitment — any of which is a corrupt repo rather
    than a key disagreement.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommitmentsCorrupt("commitments are not valid UTF-8") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CommitmentsCorrupt(f"commitments are not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CommitmentsCorrupt("commitments must be a JSON object")
    return dict(_parse_entry(raw, key) for key, raw in parsed.items())


__all__ = ["dumps_commitments", "loads_commitments"]
