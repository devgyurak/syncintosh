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
            "rotation is fast-forward only and is never merged"
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
