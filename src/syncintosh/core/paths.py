# SPDX-License-Identifier: AGPL-3.0-or-later
"""Path identity for collision detection.

macOS runs on a case-insensitive, normalization-insensitive APFS by default: `.Zshrc` and
`.zshrc` name one file, and an NFD `café` and an NFC `café` name one file too. Two records whose
paths differ only in case or Unicode normalization would therefore write **one** file twice, and
the byte comparison that catches every other duplicate path would miss it.

So the collision key normalizes to NFC and casefolds. This is a judgement about *identity*, not
about the stored path: the record keeps the exact path the user's filesystem reported, and only
the comparison is folded (``docs/design/02-base-and-merge.md``). Standard library only
(``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import unicodedata


def collision_key(path: str) -> str:
    """The identity under which two paths collide on a case- and normalization-insensitive FS."""
    return unicodedata.normalize("NFC", path).casefold()


__all__ = ["collision_key"]
