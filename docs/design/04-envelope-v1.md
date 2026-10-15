# 04 — Envelope v1

**Status:** Accepted. Depends on [03-manifest.md](03-manifest.md) (file IDs) and
[05-key-generations.md](05-key-generations.md) (key ids). Changing this note follows the
security-critical revision procedure in [README.md](README.md).

This is the one layout that is expensive to change: once ciphertext exists in a user's repo,
every byte of it is a compatibility contract.

## Layout

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `version` — envelope format version, `0x01` |
| 1 | 4 | `key_id` — key generation, **big-endian `uint32`** |
| 5 | 12 | `nonce` — random per encryption |
| 17 | n | `ciphertext` — AES-256-GCM |
| 17 + n | 16 | `tag` — GCM authentication tag |

`header = version || key_id` — the first five bytes.

- **`version` and `key_id` are separate axes.** `version` is the *format*; `key_id` is the
  *key generation*. Folding the generation into the version byte would make a key rotation
  look like a format change and would couple two things that move independently.
- **`key_id` is cleartext and belongs in the header, not the manifest.** The manifest is
  encrypted too and has to announce the generation it itself was encrypted with, so the value
  must be readable *before* decrypting. Putting it in the manifest would be circular.
- Generation `0` is invalid; ids start at 1 and grow monotonically
  ([05-key-generations.md](05-key-generations.md)).

## AAD

The additional authenticated data is a **length-prefixed** encoding of three fields:

```
aad(domain, header, subject) =
      be32(len(domain))  || domain
   || be32(len(header))  || header
   || be32(len(subject)) || subject
```

| Domain | Used for | `subject` |
|---|---|---|
| `object` | `objects/<file_id>.enc` | the 32 ASCII bytes of the file ID |
| `manifest` | `manifest.enc` | `b""` — there is exactly one manifest |
| `key-wrap` | `keys/<generation>.wrap` | the 4-byte big-endian generation being wrapped |

- **Length prefixes, not plain concatenation.** `header || file_id` has ambiguous boundaries
  as soon as either field varies in length — two different pairs could produce identical
  bytes, which is exactly the kind of collision an attacker looks for. Length prefixes make
  every `(domain, header, subject)` triple map to exactly one byte string.
- **The whole header is authenticated**, `key_id` included, so there is no unauthenticated
  cleartext field. Flipping `key_id` cannot silently select a different key: it changes the
  AAD and the tag check fails.
- **The domain label prevents slot moves.** A manifest ciphertext cannot be dropped into an
  object slot, an object cannot be moved to another object's slot (its `file_id` is in the
  AAD), and a key wrap cannot be swapped for another generation's.

### Rotation and the AAD

A rotation changes `key_id` in every header, so the header bytes change and **the AAD bytes
change for every object**. What stays fixed is the *composition rule* — same domain, same
header shape, same file ID. File IDs do not change, so nothing is renamed and git sees a
content rewrite, not a delete/add churn ([05-key-generations.md](05-key-generations.md)).

### What the AAD does not prove

The AAD binds a ciphertext to its slot, its header, and its generation, so a *substituted*
object fails. It says nothing about *when* a ciphertext was written: an old `manifest.enc` and
its matching `objects/<id>.enc` replayed as a new commit are authentic and stale. Freshness is
out of scope for the envelope ([05-key-generations.md](05-key-generations.md)); the record
digest in [03-manifest.md](03-manifest.md) narrows the gap to a *consistent* replay, but does
not close it.

## Key derivation

Per generation, subkeys are derived from the generation key `KG_g` by HKDF-SHA256, with
`salt = be32(key_id)` and a distinct `info` per purpose:

| Subkey | `info` |
|---|---|
| `K_object` | `b"syncintosh/v1/object"` |
| `K_manifest` | `b"syncintosh/v1/manifest"` |
| `K_wrap` | `b"syncintosh/v1/key-wrap"` |

An object key can never decrypt a manifest, even if the AAD were somehow satisfied.

## API

```python
def encrypt(plaintext: bytes, *, file_id: str, key: bytes, key_id: int, domain: Domain) -> bytes: ...
def decrypt(blob: bytes, *, file_id: str, keys: KeyStore, domain: Domain) -> bytes: ...
```

`decrypt` reads `version` and `key_id` from the blob header, asks the `KeyStore` for that
generation, rebuilds the AAD, and authenticates. **`file_id` is an explicit argument** — the
AAD cannot be rebuilt without it, so it may not be implicit.

`encrypt` never writes the AAD and `decrypt` never trusts one from the caller: both derive it
from the same function, which is what makes the binding real.

## Failures are distinct

Each failure means a different thing and needs a different user action, so they are separate
exceptions. Collapsing them into one error would tell the user "corrupt" when the fix is
"import a key".

| Failure | Exception | Meaning | User action |
|---|---|---|---|
| `version` unknown | `UnsupportedEnvelopeVersion` | this build is older than the object | upgrade |
| `key_id` not in this machine's ring | `UnknownKeyGeneration` | **"this machine does not hold generation N"** — not unrecoverable | `key import`, or ask another Mac to distribute it |
| the store holds generation N but its entry is unreadable | `KeyStoreCorrupt` | the keyring entry (or its index) is not valid hex / not 32 bytes — the generation **exists**, but is corrupt | re-import it; do not treat the ciphertext as tampered ([05-key-generations.md](05-key-generations.md)) |
| the ring's key for N does not match the published commitment for N | `KeyGenerationMismatch` | the published commitment for N is not this machine's key for N — **either** two machines built different keys for one generation id, **or** `keys/commitments.json` was edited (it is unauthenticated cleartext) | stop and investigate; do not merge ([05-key-generations.md](05-key-generations.md)) |
| GCM tag / AAD mismatch | `AuthenticationError` | tampered, truncated, or moved to another slot | do not trust the file; restore from another clone |
| KDF unwrap fails | `MasterKeyUnwrapError` | wrong passphrase, or a damaged export | retry the passphrase |

`KeyGenerationMismatch` lands with the rotation/commitment slice (**#8**), because nothing
publishes a commitment until then ([05-key-generations.md](05-key-generations.md)). Every
other error in this table ships with **#2**.

## Test vectors

The envelope test uses a golden fixture at `tests/fixtures/envelope_vectors.json`. The test
recomputes its `aad` and `blob` values from raw `cryptography` primitives, so the vector is a
contract rather than a snapshot of the implementation. The nonce is fixed **only inside a
vector** — production nonces are always random (§3.1), and no counter is ever used.

```json
{
  "domain": "object",
  "file_id": "0f3c…32 hex characters…",
  "key_id": 1,
  "generation_key": "…32 bytes, hex…",
  "nonce": "…12 bytes, hex…",
  "plaintext": "…",
  "aad": "…hex…",
  "blob": "…hex…"
}
```

At minimum the vectors cover: an `object` and a `manifest` vector; a tampered `blob` byte
(`AuthenticationError`); a `blob` whose `key_id` was flipped (`AuthenticationError`, or
`UnknownKeyGeneration` if the ring lacks the flipped id); and an object blob presented to
`decrypt` with a *different* `file_id` (`AuthenticationError`).