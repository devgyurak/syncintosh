# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain errors.

These live in `core` so `services` and `cli` can branch on a failure — "this machine does not
hold generation N, import it" is a different user action from "the ciphertext is corrupt", and
"the manifest is corrupt" is different again — without importing an infrastructure layer
(``AGENTS.md`` §2.2). Standard library only.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every domain failure that crosses a layer boundary."""


class ConflictsNotCovered(DomainError):
    """A resolve run has no decision for some pending conflicts, so nothing is published."""


class CryptoError(DomainError):
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


class KeyStoreCorrupt(CryptoError):
    """A stored generation entry, or the keyring index, is unreadable or the wrong shape.

    Deliberately distinct from `UnknownKeyGeneration`: the generation exists, it is corrupt,
    and the fix is to re-import it — not to report the ciphertext as tampered.
    """


class KeyGenerationMismatch(CryptoError):
    """A generation id names a different key here than the one the repo published.

    The message states only what was **observed** — "the published commitment for generation N
    is not this machine's key for N" — never that two machines created different keys:
    `keys/commitments.json` is unauthenticated cleartext, so anyone who can write the remote can
    raise this by editing it. The action is the same either way: stop and look, do not merge
    (``docs/design/05-key-generations.md``).
    """

    def __init__(self, key_id: int, detail: str) -> None:
        self.key_id = key_id
        self.detail = detail
        super().__init__(f"generation {key_id}: {detail}")


class CommitmentsCorrupt(DomainError):
    """`keys/commitments.json` is not a valid commitment map.

    Distinct from `KeyGenerationMismatch`: the file cannot be read at all (bad JSON, a bad id,
    a bad digest), which is a corrupt repo rather than a key disagreement.
    """


class ManifestError(DomainError):
    """Base class for every manifest failure."""


class ManifestCorrupt(ManifestError):
    """The manifest bytes, or a record in them, are not a valid manifest.

    Raised both when parsing untrusted bytes and when a record fails a field rule, so a caller
    can report "this repo's manifest is corrupt" without distinguishing an I/O boundary.
    """


class UnsupportedManifestSchema(ManifestError):
    """The manifest's `schema` is newer than this build understands.

    A schema bump is a migration, not a routine change (``docs/design/03-manifest.md``), so it
    is distinct from a corrupt manifest — the fix is to upgrade, not to repair.
    """

    def __init__(self, schema: int) -> None:
        self.schema = schema
        super().__init__(f"manifest schema {schema} is not supported by this build")


class GenerationDivergence(ManifestError):
    """`local` and `remote` were written under different key generations.

    Rotation is fast-forward only and is never merged (``docs/design/05-key-generations.md``),
    so two sides on different generations are not reconciled here.
    """

    def __init__(self, local_generation: int, remote_generation: int) -> None:
        self.local_generation = local_generation
        self.remote_generation = remote_generation
        super().__init__(
            f"local key generation {local_generation} diverges from remote {remote_generation}; "
            "rotation is fast-forward only and is never merged. If a local rotation was never "
            "published, run `syncintosh key rotate` — it adopts the remote and discards it"
        )


class SecretScanError(DomainError):
    """The secret scan refused a plaintext before it reached the encrypt boundary.

    Carries no secret value: a scanner is expected to name the location, never the matched
    bytes (``AGENTS.md`` §3.4, §4 "Never log secrets").
    """


class ContentDigestMismatch(DomainError):
    """A plaintext handed to the encrypt boundary does not match its record digest.

    The digest is the mix-and-match detector (``docs/design/03-manifest.md``): sealing a
    plaintext whose digest disagrees with its record would commit an object and a manifest
    that can never agree, and the no-rewrite rule would never revisit it.
    """

    def __init__(self, file_id: str, expected: str, actual: str) -> None:
        self.file_id = file_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"content for {file_id} has sha256 {actual}, expected {expected}")


class ContentParseError(DomainError):
    """A format adapter could not parse a document as its format.

    Raised by an adapter's ``parse`` when the text is malformed for that format (including a
    duplicate key, which JSONC silently folds). The content merge turns it into a conflict: two
    sides that no longer parse as the same structure cannot be merged key by key
    (``docs/design/03-manifest.md``).
    """


class RenderRefused(ContentParseError):
    """An adapter refused to render a merged value, for a reason it can name without echoing a value.

    ``reason`` is a fixed text the adapter owns, never built from a setting's key or value, so a caller may show it
    to a person. A caller that does not read the reason sees the same exception as any other render refusal.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProjectionError(DomainError):
    """A file could not be reduced to, or restored from, the preset's projected form.

    Raised on the collect side when a tracked file does not parse as its catalog format, when
    no adapter exists for that format, or when stripping the preset's keys does not render
    cleanly; and on the apply side when re-attaching this machine's local values would not
    round-trip back to the published object. Every caller treats it as fail-closed: collect
    reports the path as unprojectable rather than sealing the raw file, and apply aborts rather
    than overwrite a local value it could not preserve (``docs/design/02-base-and-merge.md``).
    """


class SyntaxInvalid(DomainError):
    """A `SyntaxValidator` refused a would-be result.

    Carries the subject and a one-line reason, never the offending text: the file is the user's
    settings and can hold a secret (``AGENTS.md`` §4). A file that fails its validator is
    reported — as a conflict when a merge produced it, and as invalid when apply would have
    written it — so nothing that would not run reaches a live path.
    """


class GitError(DomainError):
    """A git plumbing command failed.

    The transport raises this instead of leaking a `subprocess.CalledProcessError`, so a caller
    can report "git failed" without importing the subprocess layer (``AGENTS.md`` §2.2).
    """


class SyncLockError(DomainError):
    """The advisory sync lock could not be taken within the bounded wait.

    Deliberately a domain error rather than a bare timeout: the user action is "another sync is
    running — wait for it", not "fix a bug" (``docs/design/01-sync-model.md`` step 1).
    """


class PendingConflict(DomainError):
    """A previous sync stopped with unresolved conflicts, so a new merge must not start.

    Only `resolve` (and `rollback`) may run until it is cleared
    (``docs/design/01-sync-model.md``).
    """


class WatchError(DomainError):
    """The file watch could not be established, so there is nothing to wait on.

    Deliberately a domain error rather than a bare `OSError`: it is the one failure `watch` does
    **not** recover from. The loop is built to keep going through a failed cycle, so returning
    normally after a failed start would report success — and a supervisor (launchd `KeepAlive`,
    issue #8) restarts on a failed exit, not on exit 0 (``services/watch.py``).
    """


class InitRefused(DomainError):
    """`init` would clobber a key or a recovery export that already exists.

    Refused before anything is written, so a re-run can never leave the only recovery export
    holding a key the keyring does not (``AGENTS.md`` §6 — the master key is unrecoverable if
    lost).
    """


class ConfigError(DomainError):
    """The machine's `config.json` is missing, unreadable, or not a valid configuration."""


class PruneRefused(DomainError):
    """`key prune` refused a generation reachable history still needs.

    Generations are retained by default, and prune frees almost nothing: without rewriting history
    every commit stays reachable, so the only generation it can drop is one that was published but
    never used — an aborted rotation (``docs/design/05-key-generations.md``).
    """


class DaemonError(DomainError):
    """The launchd job could not be installed or removed.

    The plist embeds the absolute path of the entry point (``docs/design/README.md``, launchd), so
    a `launchctl` failure means the daemon is not running the build the user just installed.
    """


class ScanAllowCorrupt(DomainError):
    """The machine-local scan allowlist is unreadable or not a valid allowlist.

    The allowlist holds the keyed fingerprints the secret scan skips (``system/scan_allow.py``).
    It lives outside the repo and is never synced, so a failure here is local repair, not a
    disagreement with another machine — and it must never be silently reset, or every span the
    user allowed would be refused again without explanation.
    """


class LedgerCorrupt(DomainError):
    """`applied.json` is not a valid apply ledger."""


class PendingCorrupt(DomainError):
    """The pending state under `pending/` is not a valid pending record."""
