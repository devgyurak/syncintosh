# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crypto domain errors.

These live in `core` so `services` and `cli` can branch on a failure — "this machine does not
hold generation N, import it" is a different user action from "the ciphertext is corrupt" —
without importing the `crypto` infrastructure layer (``AGENTS.md`` §2.2). Standard library
only.
"""

from __future__ import annotations


class CryptoError(Exception):
    """Base class for every crypto failure."""


class AuthenticationError(CryptoError):
    """Ciphertext, tag, or AAD did not authenticate: tampered, truncated, or moved slots."""


class UnsupportedEnvelopeVersion(CryptoError):
    """The blob's format version is newer than this build understands."""


class UnknownKeyGeneration(CryptoError):
    """This machine does not hold the generation named in the envelope header.

    Deliberately distinct from `AuthenticationError`: the fix is to import the key, not to
    treat the file as corrupt (``docs/design/04-envelope-v1.md``).
    """

    def __init__(self, key_id: int) -> None:
        self.key_id = key_id
        super().__init__(f"this machine does not hold key generation {key_id}; run `key import`")


class MasterKeyUnwrapError(CryptoError):
    """The ring export could not be unwrapped: a wrong passphrase, or a damaged blob."""
