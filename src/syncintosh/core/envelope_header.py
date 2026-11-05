# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cleartext envelope header: the one place its shape is defined.

Every stored object starts with `version(1B) | key_id(4B, big-endian)` (``docs/design/04-envelope-v1.md``).
The header is pure stdlib so the crypto layer (which builds it) and the git transport (which reads
it to decide key reachability, with no key and without importing `cryptography`) share a single
definition. A format change then cannot make `key prune` miss a generation a stored envelope still needs.
"""

from __future__ import annotations

from typing import Final

ENVELOPE_VERSION: Final = 1
KEY_ID_SIZE: Final = 4
HEADER_SIZE: Final = 1 + KEY_ID_SIZE  # version(1) + key_id(4)


def header_generation(blob: bytes) -> int | None:
    """The cleartext key generation an envelope names, or `None` when the bytes are not one.

    Reads only the fixed header, so it needs **no key** — which is what lets `key prune` decide
    reachability without one (``docs/design/05-key-generations.md``). A blob shorter than the
    header, of an unknown version, or naming the invalid generation 0 is not an envelope.
    """
    if len(blob) < HEADER_SIZE or blob[0] != ENVELOPE_VERSION:
        return None
    key_id = int.from_bytes(blob[1:HEADER_SIZE], "big")
    return None if key_id == 0 else key_id
