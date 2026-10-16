# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable file identifiers.

A tracked file's ``file_id`` is 32 lowercase hex characters (``secrets.token_hex(16)``), fixed
width, generated **once** and reused forever (``docs/design/03-manifest.md``). The pattern
lives here, in ``core``, so the envelope's AAD subject rule and the manifest model cannot drift
apart. Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import re

FILE_ID_HEX_LENGTH = 32

# The 32-lowercase-hex pattern, defined once. `core/repo.py` composes it into the object slot
# path so the width cannot drift between the two.
FILE_ID_PATTERN = r"[0-9a-f]{32}"

# Exactly 32 lowercase hex characters; `fullmatch` keeps a trailing prefix from matching.
FILE_ID_RE = re.compile(FILE_ID_PATTERN)


def is_file_id(value: str) -> bool:
    """True when `value` is exactly 32 lowercase hex characters."""
    return FILE_ID_RE.fullmatch(value) is not None
