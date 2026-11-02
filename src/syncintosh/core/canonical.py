# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical JSON: one byte string per logical value.

A leaf module (it imports only `errors`) so the manifest codec, the commitments codec, and the policy digest
can all share one encoder without importing each other (``docs/design/03-manifest.md``, "Canonical
serialization"). Standard library ``json`` only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import json

from syncintosh.core.errors import ManifestCorrupt


def _check_canonical(value: object, where: str) -> None:
    """Reject a value the canonical encoder would not round-trip (float, negative int, ...)."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if value < 0:
            raise ManifestCorrupt(f"{where} holds a negative integer: {value}")
        return
    if isinstance(value, list):
        for item in value:
            _check_canonical(item, where)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ManifestCorrupt(f"{where} has a non-string key: {key!r}")
            _check_canonical(item, where)
        return
    raise ManifestCorrupt(f"{where} holds a value that is not canonical JSON: {type(value).__name__}")


def canonical_json_bytes(value: object, where: str = "manifest") -> bytes:
    """Encode `value` as canonical JSON, UTF-8, with no trailing newline.

    Pure and total for the manifest's value domain; a float, a negative int, or any other
    non-canonical type is a `ManifestCorrupt` rather than a silent re-serialization.
    """
    _check_canonical(value, where)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ManifestCorrupt("manifest holds a string that is not encodable as UTF-8") from exc


__all__ = ["canonical_json_bytes"]
