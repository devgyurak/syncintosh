# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the key-lifecycle use-cases.

The load-bearing property for `init`: it does not complete without a verified recovery export,
and a failed export leaves the keyring untouched so a retry starts clean. Cheap scrypt
parameters are passed explicitly; the production default is never reduced (``AGENTS.md`` §3.1).
"""

from __future__ import annotations

import pytest

from syncintosh.core.commitments import loads_commitments
from syncintosh.core.errors import MasterKeyUnwrapError
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.services.keys import init_new_repo

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
RECOVERY_PATH = "/home/user/recovery.sntr"
KEY_OTHER = b"\x03" * 32


class InMemoryRepoFiles:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def read(self, relpath: str) -> bytes | None:
        return self.files.get(relpath)

    def write(self, relpath: str, data: bytes) -> None:
        self.files[relpath] = data

    def delete(self, relpath: str) -> None:
        self.files.pop(relpath, None)

    def list_paths(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(path for path in self.files if path.startswith(prefix)))


class InMemoryLive:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def read(self, path: str) -> bytes | None:
        return self.files.get(path)

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def exists(self, path: str) -> bool:
        return path in self.files

    def is_symlink(self, path: str) -> bool:
        return False

    def remove(self, path: str) -> None:
        self.files.pop(path, None)


class VanishingLive(InMemoryLive):
    """A live store that accepts a write but reads back nothing."""

    def read(self, path: str) -> bytes | None:
        return None


class CorruptingLive(InMemoryLive):
    """A live store that hands back different bytes than were written."""

    def read(self, path: str) -> bytes | None:
        data = super().read(path)
        return None if data is None else data[:-1] + bytes([data[-1] ^ 0xFF])


class SubstitutingLive(InMemoryLive):
    """A live store that writes a valid export of a different ring instead."""

    def __init__(self, codec: KeyMaterial) -> None:
        super().__init__()
        self._codec = codec

    def write(self, path: str, data: bytes) -> None:
        super().write(path, self._codec.wrap(((1, KEY_OTHER),), PASSPHRASE))


class FailingLive(InMemoryLive):
    """A live store on which the write itself fails."""

    def write(self, path: str, data: bytes) -> None:
        raise OSError("disk full")


def _codec() -> KeyMaterial:
    return KeyMaterial(params=CHEAP, allowed=ALLOWED)


def test_init_creates_generation_one_and_records_the_commitment(memory_key_store) -> None:
    # given
    store = memory_key_store()
    files, live = InMemoryRepoFiles(), InMemoryLive()
    codec = _codec()

    # when
    init_new_repo(store=store, codec=codec, files=files, live=live, recovery_path=RECOVERY_PATH, passphrase=PASSPHRASE)

    # then
    assert store.generations() == (1,)
    key = store.get(1)
    assert key is not None
    stored = files.read(COMMITMENTS_PATH)
    assert stored is not None
    assert loads_commitments(stored) == {1: codec.commitment(key)}
    recovery = live.read(RECOVERY_PATH)
    assert recovery is not None
    assert codec.unwrap(recovery, PASSPHRASE) == ((1, key),)


def test_init_aborts_when_the_export_cannot_be_written(memory_key_store) -> None:
    # given
    store = memory_key_store()
    files = InMemoryRepoFiles()

    # when & then
    with pytest.raises(OSError):
        init_new_repo(
            store=store,
            codec=_codec(),
            files=files,
            live=FailingLive(),
            recovery_path=RECOVERY_PATH,
            passphrase=PASSPHRASE,
        )
    assert files.files == {}
    assert store.generations() == ()


def test_init_aborts_when_the_export_cannot_be_read_back(memory_key_store) -> None:
    # given
    files = InMemoryRepoFiles()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        init_new_repo(
            store=memory_key_store(),
            codec=_codec(),
            files=files,
            live=VanishingLive(),
            recovery_path=RECOVERY_PATH,
            passphrase=PASSPHRASE,
        )
    assert files.files == {}


def test_init_aborts_when_the_export_does_not_verify(memory_key_store) -> None:
    # given
    files = InMemoryRepoFiles()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        init_new_repo(
            store=memory_key_store(),
            codec=_codec(),
            files=files,
            live=CorruptingLive(),
            recovery_path=RECOVERY_PATH,
            passphrase=PASSPHRASE,
        )
    assert files.files == {}


def test_init_aborts_when_the_export_holds_a_different_ring(memory_key_store) -> None:
    # given — the export unwraps cleanly but describes a different ring
    files = InMemoryRepoFiles()
    codec = _codec()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        init_new_repo(
            store=memory_key_store(),
            codec=codec,
            files=files,
            live=SubstitutingLive(codec),
            recovery_path=RECOVERY_PATH,
            passphrase=PASSPHRASE,
        )
    assert files.files == {}


def test_init_leaves_the_ring_untouched_when_the_export_fails(memory_key_store) -> None:
    # given — a store that must stay empty so a retry can mint generation 1 cleanly
    store = memory_key_store()

    # when & then
    with pytest.raises(MasterKeyUnwrapError):
        init_new_repo(
            store=store,
            codec=_codec(),
            files=InMemoryRepoFiles(),
            live=VanishingLive(),
            recovery_path=RECOVERY_PATH,
            passphrase=PASSPHRASE,
        )
    assert store.generations() == ()

    # and a retry with a working store succeeds
    init_new_repo(
        store=store,
        codec=_codec(),
        files=InMemoryRepoFiles(),
        live=InMemoryLive(),
        recovery_path=RECOVERY_PATH,
        passphrase=PASSPHRASE,
    )
    assert store.generations() == (1,)
