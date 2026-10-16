# SPDX-License-Identifier: AGPL-3.0-or-later
"""Key commitments: a keyed PRF published cleartext, so "same id, different key" is visible.

`commitment(n) = HMAC-SHA256(KG_n, b"syncintosh/v1/commit")`. It is the one thing that covers an
**out-of-band** rotation, where no `keys/<n>.wrap` exists to compare: the commitment exists for
every generation either way (``docs/design/05-key-generations.md``).

The mapping's shape and canonical serialization live in `core/commitments.py` (pure, stdlib);
only the keyed computation is here, behind the `RingCodec` port, so `core` never holds a key.
"""

from __future__ import annotations

import hashlib
import hmac

from syncintosh.crypto.primitives import KEY_SIZE

# A public, versioned domain string. Versioned so the commitment of a future format can never
# collide with this one.
COMMITMENT_DOMAIN = b"syncintosh/v1/commit"


def key_commitment(key: bytes) -> str:
    """The lowercase-hex commitment for a 32-byte generation key."""
    if len(key) != KEY_SIZE:
        raise ValueError(f"a generation key must be {KEY_SIZE} bytes, got {len(key)}")
    return hmac.new(key, COMMITMENT_DOMAIN, hashlib.sha256).hexdigest()


__all__ = ["COMMITMENT_DOMAIN", "key_commitment"]
