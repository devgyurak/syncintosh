# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `FileIdSource` backed by the OS CSPRNG.

`secrets.token_hex(16)` gives 128 random bits, 32 lowercase hex characters — the fixed width
the manifest contract requires (``docs/design/03-manifest.md``). A file ID is generated once
and reused forever, so this is the only source of randomness in tracking.
"""

from __future__ import annotations

import secrets


class SecureFileIdSource:
    """Mint file IDs from the OS CSPRNG."""

    def new_file_id(self) -> str:
        """Return 32 lowercase hex characters (128 random bits)."""
        return secrets.token_hex(16)
