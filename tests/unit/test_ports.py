# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ports declared in ``core``."""

from __future__ import annotations

from syncintosh.core.models import LedgerEntry, PendingState, TextMerge
from syncintosh.core.ports import (
    Cipher,
    ContentAdapter,
    DenyList,
    Domain,
    FileIdSource,
    GitTransport,
    KeyStore,
    LedgerStore,
    LiveFiles,
    MergeDriver,
    PendingStore,
    PresetCatalog,
    RepoFiles,
    RingCodec,
    SecretScanner,
    SyncLock,
    SyntaxValidator,
)
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.tree import Tree


def test_merge_driver_port_is_structural() -> None:
    # given
    class FakeDriver:
        def merge(self, base: str, local: str, remote: str) -> TextMerge:
            return TextMerge(text=local, conflicted=False)

    # when
    matches = isinstance(FakeDriver(), MergeDriver)

    # then
    assert matches is True


def test_content_adapter_port_is_structural() -> None:
    # given
    class FakeAdapter:
        def parse(self, text: str) -> Tree:
            return Tree.create({})

        def render(self, tree: Tree, template: str) -> str:
            return template

    # when
    matches = isinstance(FakeAdapter(), ContentAdapter)

    # then
    assert matches is True


def test_a_parser_without_render_does_not_conform() -> None:
    # given — both halves of the adapter contract are required
    class ParseOnly:
        def parse(self, text: str) -> Tree:
            return Tree.create({})

    # when
    matches = isinstance(ParseOnly(), ContentAdapter)

    # then
    assert matches is False


def test_key_store_port_is_structural() -> None:
    # given
    class FakeKeyStore:
        def generations(self) -> tuple[int, ...]:
            return ()

        def get(self, key_id: int) -> bytes | None:
            return None

        def put(self, key_id: int, key: bytes) -> None:
            return None

    # when
    matches = isinstance(FakeKeyStore(), KeyStore)

    # then
    assert matches is True


def test_key_store_without_put_does_not_conform() -> None:
    # given — a single-key store must not satisfy the generation-addressed port
    class SingleKeyStore:
        def generations(self) -> tuple[int, ...]:
            return ()

        def get(self, key_id: int) -> bytes | None:
            return None

    # when
    matches = isinstance(SingleKeyStore(), KeyStore)

    # then
    assert matches is False


def test_domain_literal_covers_exactly_the_three_slots() -> None:
    # when
    domains: tuple[Domain, ...] = ("object", "manifest", "key-wrap")

    # then
    assert domains == ("object", "manifest", "key-wrap")


def test_file_id_source_port_is_structural() -> None:
    # given
    class FakeSource:
        def new_file_id(self) -> str:
            return "0" * 32

    # when
    matches = isinstance(FakeSource(), FileIdSource)

    # then
    assert matches is True


def test_cipher_port_is_structural() -> None:
    # given
    class FakeCipher:
        def seal(self, plaintext: bytes, *, domain, file_id, key_id) -> bytes:
            return plaintext

        def open(self, blob: bytes, *, domain, file_id) -> bytes:
            return blob

    # when
    matches = isinstance(FakeCipher(), Cipher)

    # then
    assert matches is True


def test_repo_files_port_is_structural() -> None:
    # given
    class FakeRepo:
        def read(self, relpath: str) -> bytes | None:
            return None

        def write(self, relpath: str, data: bytes) -> None:
            return None

        def delete(self, relpath: str) -> None:
            return None

        def list_paths(self, prefix: str) -> tuple[str, ...]:
            return ()

    # when
    matches = isinstance(FakeRepo(), RepoFiles)

    # then
    assert matches is True


def test_a_repo_without_list_paths_does_not_conform() -> None:
    # given — the builtin-shadowing name `list` is deliberately not the port's method
    class LooksLikeRepo:
        def read(self, relpath: str) -> bytes | None:
            return None

        def write(self, relpath: str, data: bytes) -> None:
            return None

        def delete(self, relpath: str) -> None:
            return None

    # when
    matches = isinstance(LooksLikeRepo(), RepoFiles)

    # then
    assert matches is False


def test_secret_scanner_port_is_structural() -> None:
    # given
    class FakeScanner:
        def scan(self, plaintext: bytes, *, subject: str) -> None:
            return None

    # when
    matches = isinstance(FakeScanner(), SecretScanner)

    # then
    assert matches is True


def test_git_transport_port_is_structural() -> None:
    # given
    class FakeTransport:
        def init(self) -> None: ...
        def clone(self, remote: str) -> None: ...
        def fetch(self, remote: str, branch: str) -> None: ...
        def head(self) -> str | None:
            return None

        def branch(self) -> str | None:
            return None

        def resolve(self, rev: str) -> str | None:
            return None

        def merge_bases(self, left: str, right: str) -> tuple[str, ...]:
            return ()

        def is_ancestor(self, ancestor: str, descendant: str) -> bool:
            return False

        def read_blob(self, commit: str, relpath: str) -> bytes | None:
            return None

        def commit(self, parents: tuple[str, ...], message: str) -> str:
            return "0" * 40

        def update_ref(self, branch: str, commit: str) -> None: ...

        def materialize(self, commit: str) -> None: ...

        def push(self, remote: str, branch: str, commit: str) -> str:
            return "ok"

    # when
    matches = isinstance(FakeTransport(), GitTransport)

    # then
    assert matches is True


def test_a_transport_without_push_does_not_conform() -> None:
    # given
    class NoPush:
        def init(self) -> None: ...
        def clone(self, remote: str) -> None: ...
        def fetch(self, remote: str, branch: str) -> None: ...
        def head(self) -> str | None:
            return None

        def branch(self) -> str | None:
            return None

        def resolve(self, rev: str) -> str | None:
            return None

        def merge_bases(self, left: str, right: str) -> tuple[str, ...]:
            return ()

        def is_ancestor(self, ancestor: str, descendant: str) -> bool:
            return False

        def read_blob(self, commit: str, relpath: str) -> bytes | None:
            return None

        def commit(self, parents: tuple[str, ...], message: str) -> str:
            return "0" * 40

        def update_ref(self, branch: str, commit: str) -> None: ...

    # when
    matches = isinstance(NoPush(), GitTransport)

    # then
    assert matches is False


def test_sync_lock_port_is_structural() -> None:
    # given
    class FakeLock:
        def acquire(self) -> None: ...
        def release(self) -> None: ...

    # when
    matches = isinstance(FakeLock(), SyncLock)

    # then
    assert matches is True


def test_ledger_store_port_is_structural() -> None:
    # given
    class FakeLedger:
        def load(self) -> dict[str, LedgerEntry]:
            return {}

        def save(self, entries) -> None: ...

    # when
    matches = isinstance(FakeLedger(), LedgerStore)

    # then
    assert matches is True


def test_pending_store_port_is_structural() -> None:
    # given
    class FakePending:
        def is_pending(self) -> bool:
            return False

        def record(self, state: PendingState) -> None: ...

        def clear(self) -> None: ...

    # when
    matches = isinstance(FakePending(), PendingStore)

    # then
    assert matches is True


def test_live_files_port_is_structural() -> None:
    # given
    class FakeLive:
        def read(self, path: str) -> bytes | None:
            return None

        def write(self, path: str, data: bytes) -> None: ...

        def exists(self, path: str) -> bool:
            return False

        def is_symlink(self, path: str) -> bool:
            return False

        def remove(self, path: str) -> None: ...

    # when
    matches = isinstance(FakeLive(), LiveFiles)

    # then
    assert matches is True


def test_deny_list_port_is_structural() -> None:
    # given
    class FakeDenyList:
        def is_denied(self, home: str, relpath: str) -> bool:
            return False

        def escapes(self, home: str, relpath: str) -> bool:
            return False

    # when
    matches = isinstance(FakeDenyList(), DenyList)

    # then
    assert matches is True


def test_ring_codec_port_is_structural() -> None:
    # given
    class FakeCodec:
        def wrap(self, ring, passphrase: str) -> bytes:
            return b""

        def unwrap(self, blob: bytes, passphrase: str):
            return ()

        def commitment(self, key: bytes) -> str:
            return "0" * 64

    # when
    matches = isinstance(FakeCodec(), RingCodec)

    # then
    assert matches is True


def test_preset_catalog_port_is_structural() -> None:
    # given
    class FakeCatalog:
        def policy_for(self, path: str) -> PresetFile | None:
            return None

        def knows(self, preset: str, path: str) -> bool:
            return True

        def get(self, name: str) -> Preset | None:
            return None

    # when
    matches = isinstance(FakeCatalog(), PresetCatalog)

    # then
    assert matches is True


def test_a_catalog_without_knows_does_not_conform() -> None:
    # given — the apply-side provenance check is part of the port, not optional
    class PolicyOnly:
        def policy_for(self, path: str) -> PresetFile | None:
            return None

        def get(self, name: str) -> Preset | None:
            return None

    # when
    matches = isinstance(PolicyOnly(), PresetCatalog)

    # then
    assert matches is False


def test_syntax_validator_port_is_structural() -> None:
    # given
    class FakeValidator:
        def check(self, text: str, *, subject: str) -> None:
            return None

    # when
    matches = isinstance(FakeValidator(), SyntaxValidator)

    # then
    assert matches is True
