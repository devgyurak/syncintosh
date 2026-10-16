# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `RingCodec` port over the ring export and the key commitment.

`core` and `services` never touch scrypt or AES-GCM: they hand the ascending `Ring` across this
port and get bytes back (``AGENTS.md`` §2.2). The KDF parameters are injected, so a test can run
a deliberately cheap KDF without ever weakening the shipped default — the production parameters
stay the module default and are never reduced for tests (``AGENTS.md`` §3.1).
"""

from __future__ import annotations

from syncintosh.core.ports import Ring
from syncintosh.crypto.commitments import key_commitment
from syncintosh.crypto.ring_export import PRODUCTION_ALLOWED, PRODUCTION_SCRYPT, RingEntry, ScryptParams, unwrap_ring
from syncintosh.crypto.ring_export import wrap_ring as _wrap_ring


class KeyMaterial:
    """A `RingCodec` over `crypto.ring_export` and `crypto.commitments`."""

    def __init__(
        self,
        *,
        params: ScryptParams = PRODUCTION_SCRYPT,
        allowed: frozenset[ScryptParams] = PRODUCTION_ALLOWED,
    ) -> None:
        self._params = params
        self._allowed = allowed

    def wrap(self, ring: Ring, passphrase: str) -> bytes:
        """Wrap the ascending `ring` as one export blob, with the injected KDF parameters."""
        entries = (RingEntry(key_id, key) for key_id, key in ring)
        return _wrap_ring(entries, passphrase, params=self._params, allowed=self._allowed)

    def unwrap(self, blob: bytes, passphrase: str) -> Ring:
        """Unwrap an export blob into an ascending `Ring`."""
        return tuple((entry.key_id, entry.key) for entry in unwrap_ring(blob, passphrase, allowed=self._allowed))

    def commitment(self, key: bytes) -> str:
        """The lowercase-hex commitment for a generation key."""
        return key_commitment(key)


__all__ = ["KeyMaterial"]
