# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: create a repo's first key generation, with a forced recovery export.

The master key is unrecoverable if lost (``AGENTS.md`` §6), so `init_new_repo` treats the
recovery export as part of creation, not a follow-up: it writes the export, reads it back, and
unwraps it, and only then touches the ring and records the commitment. A failed export leaves
the keyring exactly as it was, so a retry starts clean instead of colliding with an abandoned
generation 1.

`services` orchestrates the `KeyStore`, `RingCodec`, `RepoFiles`, and `LiveFiles` ports; it never
imports `crypto` (``AGENTS.md`` §2.2).
"""

from __future__ import annotations

import os

from syncintosh.core.commitments import dumps_commitments
from syncintosh.core.errors import MasterKeyUnwrapError
from syncintosh.core.ports import GENERATION_KEY_SIZE, KeyStore, LiveFiles, RepoFiles, Ring, RingCodec
from syncintosh.core.repo import COMMITMENTS_PATH


def init_new_repo(
    *,
    store: KeyStore,
    codec: RingCodec,
    files: RepoFiles,
    live: LiveFiles,
    recovery_path: str,
    passphrase: str,
) -> None:
    """Create generation 1, force a verified recovery export, and record its commitment.

    `recovery_path` is where the wrapped ring is written for the user to keep. The export is
    read back and unwrapped before the ring is touched, so `init` never leaves a key stored
    without a backup the user can open — and a failure is retryable.
    """
    generation_key = os.urandom(GENERATION_KEY_SIZE)
    ring: Ring = ((1, generation_key),)
    blob = codec.wrap(ring, passphrase)
    live.write(recovery_path, blob)
    written = live.read(recovery_path)
    if written is None:
        raise MasterKeyUnwrapError("the recovery export was not written; init is not complete")
    if codec.unwrap(written, passphrase) != ring:
        raise MasterKeyUnwrapError("the recovery export did not verify; init is not complete")

    # Only now touch the store: a store mutation before the export verifies would wedge a retry,
    # because the next attempt mints a fresh key for generation 1 and `put` refuses a second key.
    store.put(1, generation_key)
    files.write(COMMITMENTS_PATH, dumps_commitments({1: codec.commitment(generation_key)}))


__all__ = ["init_new_repo"]
