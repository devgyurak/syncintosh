# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ports implemented by infrastructure.

`core` and `services` depend on these protocols, never on a concrete adapter, cipher, git
client, or LLM (``AGENTS.md`` §2.2). Implementations live in `adapters`, `crypto`, `vcs`,
`system`, and `llm`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, runtime_checkable

from syncintosh.core.models import LedgerEntry, PendingState, TextMerge
from syncintosh.core.tree import Tree

# What an envelope binds a ciphertext to, and which subkey decrypts it
# (`docs/design/04-envelope-v1.md`). A value is serialized verbatim in the HKDF `info`, so it
# is a `Literal`, never an `Enum` (``AGENTS.md`` §4).
Domain = Literal["object", "manifest", "key-wrap"]

# How a push attempt ended (``docs/design/01-sync-model.md`` step 12–13). A non-fast-forward
# is retried against the fresh remote; anything else is reported.
PushOutcome = Literal["ok", "non-fast-forward", "rejected"]

# A generation key is 32 random bytes (``docs/design/05-key-generations.md``) — the same width
# as an AES-256 key, which is not a coincidence: it is the key AES-GCM is driven with after
# HKDF. `services` mints one without importing `crypto`, hence the constant here.
GENERATION_KEY_SIZE = 32

# The whole ring as ascending `(generation id, key)` pairs — the shape the key-lifecycle
# services pass across the `RingCodec` port without naming a crypto type (``AGENTS.md`` §2.2).
Ring = tuple[tuple[int, bytes], ...]

# The local tracking ref a fetch writes. The remote is a URL from config, not a configured
# `origin`, so the ref is named explicitly rather than inferred. It lives here so `services` can
# resolve it without importing the `vcs` layer (``AGENTS.md`` §2.2).
REMOTE_REF_PREFIX = "refs/remotes/syncintosh/"


def remote_ref(branch: str) -> str:
    """The local tracking ref a fetch of `branch` writes (``syncintosh/<branch>``)."""
    return f"{REMOTE_REF_PREFIX}{branch}"


@runtime_checkable
class MergeDriver(Protocol):
    """Line-level three-way merge.

    `merge3` and `git merge-file` are not stdlib, so they sit behind this port; `core` keeps
    only the key-level rules (``AGENTS.md`` §2.9). `conflicted` is the merge's own verdict, so
    no caller re-derives it from markers.
    """

    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        """Return the merged text and whether a conflict remains."""
        ...


@runtime_checkable
class ContentAdapter(Protocol):
    """Parses one format to a typed tree and renders a merged tree back to text.

    The adapter is where a format's *key-level* rules live: it turns text into the comparable
    :class:`~syncintosh.core.tree.Tree` the merge engine consumes, and turns the merged tree
    back into a file that preserves the original's comments, key order, and formatting
    (``AGENTS.md`` §2.5). ``render`` takes the local text as a template so a format that cannot
    be re-serialized faithfully (JSONC) is edited minimally instead of rewritten.

    Implementations live in ``adapters``; ``core`` and ``services`` depend only on this port
    (``AGENTS.md`` §2.2).
    """

    def parse(self, text: str) -> Tree:
        """Parse `text` into a typed tree, or raise when the text is not this format."""
        ...

    def render(self, tree: Tree, template: str) -> str:
        """Render `tree` into text, using `template` as the unchanged baseline to edit."""
        ...


@runtime_checkable
class KeyStore(Protocol):
    """The per-repo key ring: generation id -> 32-byte generation key.

    Addresses generations by id, not a single master key, because `merge-base` can point at a
    commit encrypted under an older generation and several generations must resolve at once
    (`docs/design/05-key-generations.md`). `drop()` is deliberately absent here: prune and the
    removal of an unpublished rotation land with the rotation slice (#8).
    """

    def generations(self) -> tuple[int, ...]:
        """Return the stored generation ids, ascending."""
        ...

    def get(self, key_id: int) -> bytes | None:
        """Return generation `key_id`, or `None` when this machine does not hold it."""
        ...

    def put(self, key_id: int, key: bytes) -> None:
        """Store a generation key. Raises on failure."""
        ...


@runtime_checkable
class FileIdSource(Protocol):
    """Mints the stable, random, fixed-width ID a newly tracked path keeps forever.

    Randomness is the OS entropy pool, which `core` may not touch, so the source lives in
    `crypto` (``AGENTS.md`` §2.1, §2.2). The port is intentionally tiny: `core` needs one fresh
    ID per new `(scope, machine, path)` and revalidates it (`docs/design/03-manifest.md`).
    """

    def new_file_id(self) -> str:
        """Return a fresh 32-lowercase-hex file ID."""
        ...


@runtime_checkable
class Cipher(Protocol):
    """Seals and opens stored objects under the current key generation.

    `services` never touches a raw key or nonce: it hands a plaintext to `seal` and gets the
    envelope bytes back (``docs/design/04-envelope-v1.md``). `open` reads the generation from
    the blob header, so only `seal` takes `key_id` explicitly.
    """

    def seal(self, plaintext: bytes, *, domain: Domain, file_id: str, key_id: int) -> bytes:
        """Encrypt `plaintext` for `domain`, binding it to `file_id` and generation `key_id`."""
        ...

    def open(self, blob: bytes, *, domain: Domain, file_id: str) -> bytes:
        """Decrypt `blob`, raising a distinct error per failure mode."""
        ...


@runtime_checkable
class RepoFiles(Protocol):
    """Reads and writes files in the repo's working tree, addressed by repo-relative path."""

    def read(self, relpath: str) -> bytes | None:
        """Return the file's bytes, or `None` when it does not exist."""
        ...

    def write(self, relpath: str, data: bytes) -> None:
        """Write `data`, creating parent directories. Raises on failure."""
        ...

    def delete(self, relpath: str) -> None:
        """Remove the file if present. Raises on any other failure."""
        ...

    def list_paths(self, prefix: str) -> tuple[str, ...]:
        """Return the repo-relative paths of existing files under `prefix`, sorted."""
        ...


@runtime_checkable
class SecretScanner(Protocol):
    """The secret scan that gates the encrypt boundary.

    Nothing reaches `Cipher.seal` without passing this (``docs/design/01-sync-model.md``,
    ``AGENTS.md`` §3.4). A refusal names the location only, never the matched bytes
    (``AGENTS.md`` §4).
    """

    def scan(self, plaintext: bytes, *, subject: str) -> None:
        """Raise `SecretScanError` when a secret is found; return `None` when clean."""
        ...


@runtime_checkable
class DenyList(Protocol):
    """The dangerous paths refused before collection, and the escape check before apply.

    A private key or a cloud credential must never enter a repo, however the user configured
    their items (``AGENTS.md`` §3.4). Judgement is by real path, so a symlink whose target is
    `~/.ssh` is still refused. `escapes` is the apply-side trust-boundary check: a record's path
    arrives from the repo, so it must be proven to stay under `$HOME` before it is written
    (``docs/design/03-manifest.md``). `system` implements this; `services` only asks.
    """

    def is_denied(self, home: str, relpath: str) -> bool:
        """True when the live path `home/relpath` must never be collected."""
        ...

    def escapes(self, home: str, relpath: str) -> bool:
        """True when resolving `home/relpath` (parent first) leaves `home`."""
        ...


@runtime_checkable
class GitTransport(Protocol):
    """The git plumbing the sync cycle uses, bound to one working tree.

    Only the commands the cycle needs, and never `pull`, `merge`, or `rebase`
    (``docs/design/01-sync-model.md``): sync decrypts both sides, merges the plaintext, and
    builds the resulting tree itself with `commit`. `fetch` never passes `--depth`, because
    `merge-base` needs history.
    """

    def init(self) -> None:
        """Create the repository in the transport's working tree. Raises on failure."""
        ...

    def clone(self, remote: str) -> None:
        """Clone `remote` into the transport's working tree, with full history."""
        ...

    def fetch(self, remote: str, branch: str) -> None:
        """Fetch `branch` from `remote` (never shallow)."""
        ...

    def head(self) -> str | None:
        """Return the current commit id, or `None` on an unborn branch."""
        ...

    def branch(self) -> str | None:
        """Return the branch HEAD points at, or `None` when it is detached.

        The name is returned even for an **unborn** branch, so a repo cloned from a remote whose
        HEAD names a branch it does not have can be put on the branch the remote actually uses.
        """
        ...

    def resolve(self, rev: str) -> str | None:
        """Return the commit id `rev` names, or `None` when it does not exist."""
        ...

    def merge_bases(self, left: str, right: str) -> tuple[str, ...]:
        """Return every merge base of `left` and `right`, sorted; empty when unrelated."""
        ...

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True when `ancestor` is reachable from `descendant`."""
        ...

    def read_blob(self, commit: str, relpath: str) -> bytes | None:
        """Return the blob at `relpath` in `commit`, or `None` when it is absent."""
        ...

    def commit(self, parents: tuple[str, ...], message: str, paths: tuple[str, ...]) -> str:
        """Stage `paths` and create a commit with `parents`; return its id.

        One parent is the collect commit, two are the merge commit
        (``docs/design/01-sync-model.md`` step 5 and 12). The pathspec is explicit, never
        `add -A` of the whole tree: the working tree is the staging area, and a file that is not
        part of the repo layout must never be swept into a commit.
        """
        ...

    def update_ref(self, branch: str, commit: str) -> None:
        """Point `refs/heads/<branch>` at `commit` without touching the working tree."""
        ...

    def materialize(self, commit: str) -> None:
        """Reset the index and working tree to `commit`'s tree.

        Used after a fast-forward moves the branch forward, so the working tree a later `commit`
        stages matches the new HEAD. It is a tree checkout, never a merge: `read-tree --reset`.
        """
        ...

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        """Push `commit` to `branch` on `remote`, classified for the retry rule."""
        ...


@runtime_checkable
class SyncLock(Protocol):
    """The advisory lock every mutating operation shares.

    Two callers (the daemon and the CLI) must not interleave, and a rotation must not race a
    sync (``docs/design/01-sync-model.md`` step 1). `acquire` waits for a bounded time and
    raises `SyncLockError` instead of racing when the deadline passes. No I/O in the
    constructor (``AGENTS.md`` §2.6).
    """

    def acquire(self) -> None:
        """Take the lock, or raise `SyncLockError` after the bounded wait."""
        ...

    def release(self) -> None:
        """Release the lock. Releasing a lock this process does not hold is not an error."""
        ...


@runtime_checkable
class LedgerStore(Protocol):
    """The apply ledger, read and written whole.

    `applied.json` lives outside the repo (``AGENTS.md`` §3.3) and is settled at the end of
    every successful cycle (``docs/design/01-sync-model.md``). `load` returns an empty map when
    no ledger exists yet.
    """

    def load(self) -> Mapping[str, LedgerEntry]:
        """Return the per-file baselines, keyed by `file_id`."""
        ...

    def save(self, entries: Mapping[str, LedgerEntry]) -> None:
        """Replace the ledger with `entries`. Raises on failure."""
        ...


@runtime_checkable
class PendingStore(Protocol):
    """The unresolved merge a stopped sync leaves behind, outside the repo.

    While a pending state exists, `sync` refuses to start a new merge; only `resolve` may run
    (``docs/design/01-sync-model.md``). The plaintext of the unresolved records lives here, and
    nowhere else on disk (``AGENTS.md`` §3.3).
    """

    def is_pending(self) -> bool:
        """True when a previous sync stopped with unresolved conflicts."""
        ...

    def record(self, state: PendingState) -> None:
        """Write `state` and its unresolved plaintexts. Raises on failure."""
        ...

    def clear(self) -> None:
        """Remove the pending state, if any. Raises on failure."""
        ...


@runtime_checkable
class LiveFiles(Protocol):
    """The user's real files, addressed by absolute path.

    `apply` writes live files through this port after the push, and the collection step reads
    them, so `services` never opens a path from an untrusted manifest directly
    (``docs/design/03-manifest.md``). `is_symlink` lets `apply` refuse to write through a link.
    """

    def read(self, path: str) -> bytes | None:
        """Return the file's bytes, or `None` when it does not exist."""
        ...

    def write(self, path: str, data: bytes) -> None:
        """Write `data`, creating parent directories. Raises on failure."""
        ...

    def exists(self, path: str) -> bool:
        """True when `path` exists (a symlink counts as existing)."""
        ...

    def is_symlink(self, path: str) -> bool:
        """True when `path` is a symbolic link."""
        ...

    def remove(self, path: str) -> None:
        """Remove the file if present. Raises on any other failure."""
        ...


@runtime_checkable
class RingCodec(Protocol):
    """Wrap and unwrap the whole key ring under a passphrase, and commit to a generation.

    `core` and `services` never touch scrypt or AES-GCM directly; `crypto` implements this
    (``docs/design/05-key-generations.md``). `commitment` is the keyed PRF published cleartext,
    so "same id, different key" is detectable without the key.
    """

    def wrap(self, ring: Ring, passphrase: str) -> bytes:
        """Wrap the ascending `ring` as one self-describing export blob."""
        ...

    def unwrap(self, blob: bytes, passphrase: str) -> Ring:
        """Unwrap an export blob, raising `MasterKeyUnwrapError` on any failure."""
        ...

    def commitment(self, key: bytes) -> str:
        """Return the lowercase-hex commitment for a generation key."""
        ...
