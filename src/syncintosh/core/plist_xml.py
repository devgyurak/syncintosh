# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one canonical XML form of a property list, shared by every reader and writer of one.

A macOS preferences domain has no stable text: `defaults export` indents with tabs and prints `<real>1.0</real>` as
`<real>1</real>`, and the file on disk is binary. The plist adapter and the defaults-domain store both reach for
this module so a document read from either, merged, and written back is the same bytes — a diff then shows only what
a person changed, never a re-indentation. Keys are sorted (`sort_keys=True`) because a plist dict has no meaningful
order; that is the one place the form differs from a text editor's, and it is what makes the form unique.

Standard library only (``AGENTS.md`` §2.1). Every refusal is a `ValueError`; the adapter turns it into its own error.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Mapping
from typing import Final
from xml.parsers.expat import ExpatError

BINARY_REFUSED: Final = "a binary property list is not handled; only the XML form is"
ROOT_REFUSED: Final = "the root of a property list must be a dict"
SUBSET_REFUSED: Final = "a DOCTYPE with an internal subset is refused"
MALFORMED_REFUSED: Final = "not a well-formed XML property list"

_BINARY_MAGIC: Final = b"bplist"
# `<!DOCTYPE plist [ ... ]>`: an internal subset can declare entities, which the XML parser would expand. The
# external `PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://..."` form Apple writes has no `[` and is accepted
# (plistlib never fetches it).
_INTERNAL_SUBSET: Final = re.compile(rb"<!DOCTYPE[^>\[]*\[", re.IGNORECASE)


def parse_plist(data: bytes) -> dict[str, object]:
    """The dict an XML property list holds; `ValueError` for a binary plist, a non-dict root, or an internal subset."""
    if data.lstrip().startswith(_BINARY_MAGIC):
        raise ValueError(BINARY_REFUSED)
    if _INTERNAL_SUBSET.search(data):
        raise ValueError(SUBSET_REFUSED)
    try:
        value = plistlib.loads(data, fmt=plistlib.FMT_XML)
    except (ValueError, OverflowError, ExpatError) as exc:
        # expat's error type is not a `ValueError`; every failure here means "not a plist we can read".
        raise ValueError(MALFORMED_REFUSED) from exc
    if not isinstance(value, dict):
        raise ValueError(ROOT_REFUSED)
    return value


def dump_plist(value: Mapping[str, object]) -> bytes:
    """The canonical XML form of `value`: sorted keys, XML format, Apple's header."""
    try:
        return plistlib.dumps(dict(value), fmt=plistlib.FMT_XML, sort_keys=True)
    except (TypeError, OverflowError) as exc:
        raise ValueError(MALFORMED_REFUSED) from exc


def canonical_plist(data: bytes) -> bytes:
    """`data` re-serialized in the canonical form; a second call returns the same bytes."""
    return dump_plist(parse_plist(data))


__all__ = ["BINARY_REFUSED", "ROOT_REFUSED", "SUBSET_REFUSED", "canonical_plist", "dump_plist", "parse_plist"]
