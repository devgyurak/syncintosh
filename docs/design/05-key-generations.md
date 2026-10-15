# 05 — Key generations

**Status:** Accepted. Depends on [04-envelope-v1.md](04-envelope-v1.md).

## The key ring

A repo has one **key ring**: an ordered set of generation keys `KG_1 … KG_n`, each 32 random
bytes, addressed by generation id. A single machine can hold several at once, because
`merge-base` can point at a commit encrypted under an older generation.

```python
class KeyStore(Protocol):
    def generations(self) -> tuple[int, ...]: ...  # stored generations, ascending
    def get(self, key_id: int) -> bytes | None: ...  # None = this machine does not hold it
    def put(self, key_id: int, key: bytes) -> None: ...  # returns None, raises on failure
```

**This shape is fixed now** even though the port change lands in #2, because it is expensive
to change once objects are stored: a single-key `get()` cannot resolve a `key_id`, and
retro-fitting one means rewriting every stored object. (The port that exists today is the
single-key `get()`/`set()` pair.)

`drop(key_id)` — explicit `key prune`, and the removal of an unpublished generation after a
discarded rotation — is added in **#8**, together with the reachability walk that decides
whether a generation is still needed. #2 declares `generations`/`get`/`put` only; nothing in
#2 may drop a generation.

`get()` returning `None` for a generation this machine does not hold is what makes
`UnknownKeyGeneration` say *"import it"* rather than *"unrecoverable"*
([04-envelope-v1.md](04-envelope-v1.md)).

**`KG_1` is the master key.** `init` creates it, the recovery-key backup protects it, and
§3.1's "wrapped with a KDF for export/import" applies to it. `key export` wraps the **whole
ring** — `KG_1` first, then every retained generation — with scrypt
([Ring export](#ring-export) below), so exporting and importing the ring is one operation and
"lose the master key" and "lose the ring" are the same failure. The KDF parameters are never
reduced for tests (§3.1).

Late generations are **independent random keys**, never derived from an earlier generation. If
`KG_{n+1}` were `HKDF(KG_n, …)`, then anyone holding `KG_n` could derive `KG_{n+1}`, and a
rotation after a device was removed would protect nothing.

## Ring export

`key export` writes the **whole ring** as one self-describing blob; `key import` reads it back.
The format is fixed, and it is self-describing so an import can reject an unknown format or a
KDF it will not run **before** spending work on it:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `magic` — `"SNTR"`, ASCII |
| 4 | 1 | `format` — `0x01` |
| 5 | 1 | `kdf` — `0x01` = scrypt |
| 6 | 1 | `log2_n` — scrypt cost exponent |
| 7 | 4 | `r` — scrypt block-size factor, big-endian `uint32` |
| 11 | 4 | `p` — scrypt parallelization, big-endian `uint32` |
| 15 | 16 | `salt` — scrypt salt |
| 31 | 12 | `nonce` — AES-256-GCM nonce |
| 43 | n | `ciphertext` |
| 43 + n | 16 | `tag` — GCM authentication tag |

`header = bytes 0…30` (`magic || format || kdf || log2_n || r || p || salt`).

- **AAD = length-prefixed `("ring-export", header)`** — the same composition rule as the
  envelope ([04-envelope-v1.md](04-envelope-v1.md)), with its own domain label so an export
  blob can never be mistaken for a stored object or a key wrap.
- **plaintext = `count(be32) || (key_id(be32) || KG(32))*`**, ascending by `key_id`, so `KG_1`
  is first. `count = 0` is invalid — a ring always holds generation 1.
- The KDF key is `scrypt(passphrase, salt, N=2^log2_n, r, p, length=32)`. Production is
  **scrypt `log2_n = 17, r = 8, p = 1`** (≈128 MiB, §3.1). An import runs the KDF **only**
  when the serialized parameters are in an allowlist, so a tampered blob cannot force an
  expensive derivation (DoS) and a deliberately weak export is refused.
- **Failure is one error.** A wrong passphrase and a damaged blob both surface as
  `MasterKeyUnwrapError` ([04-envelope-v1.md](04-envelope-v1.md)) — the user retries the
  passphrase either way.

## Retention

- **Generations are retained by default.** There is no automatic expiry.
- **Prune is explicit** (`key prune <generation>`) and must refuse while the generation is
  still needed. *Needed* means reachable: some commit reachable from this machine's `HEAD` or
  from `origin/<branch>` has a tree holding an object encrypted under it.
- **Reachability frees almost nothing.** Without rewriting history, every commit stays
  reachable, so the only generation prune can drop is one that was published but never used
  (an aborted rotation). Say this in the `--help` text, so nobody expects prune to shrink the
  ring — the alternative, garbage-collecting old commits, means force-pushing, which §7
  denies.

## Rotation

Rotation is one operation:

1. Generate `KG_{n+1}` (random, 32 bytes) and add it to the ring.
2. Set the manifest's `key_generation` to `n+1`.
3. Re-encrypt **every** object and the manifest under the new generation.

That is one full-rewrite commit, and it is accepted — it is a rare, deliberate action.

- **AAD bytes change; the AAD rule does not.** `key_id` is authenticated, so every header
  changes and therefore every AAD changes. The composition rule, the domains, and the file IDs
  stay the same ([04-envelope-v1.md](04-envelope-v1.md)).
- **Old generations stay in the ring**, so this machine's history — and any commit another
  Mac has not fetched yet — remains decryptable.

### Rotation is exclusive, and fast-forward only

- Rotation takes the same lock as `sync` ([01-sync-model.md](01-sync-model.md)), so it cannot
  race a sync.
- A rotation commit is published **fast-forward only**. On a non-fast-forward push, discard
  the rotation and restart against the fresh `origin/<branch>`; **never merge two rotations**.
  Discarding must also **remove the not-yet-published generation from the ring**. Leaving it
  there is worse than untidy: the abandoned `KG_n` would answer `get(n)` for the *other*
  machine's genuine generation `n`, and every one of its objects would fail authentication as
  if it had been tampered with.
- Two different keys for one generation id is a **hard error**, not something to reconcile:
  the machines disagree about a key that published objects are already bound to.

### Key commitments make "same id, different key" detectable

`keys/commitments.json` — cleartext, canonical JSON — maps every generation id to

```
commitment(n) = HMAC-SHA256(KG_n, b"syncintosh/v1/commit")
```

A commitment must be readable **without** the key: the manifest that would otherwise carry the
list is itself encrypted *under* the generation in question. It leaks nothing — a PRF output
over a public string, keyed by 256 random bits — so it can live in the repo as plaintext.

On every fetch, a machine compares the published commitment for each id it holds:

- equal → the ids name the same key, as they should be;
- **different** → `KeyGenerationMismatch` ([04-envelope-v1.md](04-envelope-v1.md)); stop and
  investigate, and do not merge.

The message must **not** assert that two machines created different keys. `commitments.json`
is unauthenticated cleartext, so anyone who can write to the remote can raise this error by
editing it — a denial of service, not a confidentiality break, and no different from tampering
with any other file the user does not control. State what was observed ("the published
commitment for generation N is not this machine's key for N") and leave the cause open; the
action, stop and look, is the same either way.

This is what covers an **out-of-band** rotation: a removal or a compromise rotation publishes
no `keys/<n>.wrap`, so a wrap comparison has nothing to look at, while the commitment exists
for every generation either way.

When a locally held generation's commitment mismatches, it may be **replaced** by the
published one only while no published object is encrypted under the local one — which is
exactly the discarded-rotation case. Otherwise it is the hard error above.

## Threat model — one line that matters

**Rotation protects future data only.** Ciphertext already in git history stays decryptable
with the old key, so rotation is the right answer to "this machine is being retired" and the
wrong answer to "the key leaked". It also does not remove anyone's access by itself — a
removed machine still holds the old key, which is why its new key must not travel through the
repo ([the distribution table above](#distributing-a-new-generation)).

**On compromise, migrate to a new repo.** Force-pushing to drop old commits is not an option:
§7 denies force-push, and the merge-base model needs shared history
([01-sync-model.md](01-sync-model.md)). A fresh repo with a fresh generation 1 is the
supported answer.

### Not mitigated: rollback (replay)

An attacker who controls the remote can present an **old** `manifest.enc`, or an old
`objects/<id>.enc`, as a new commit. The AAD cannot stop this: the ciphertext is authentic, it
is merely old. The record digest ([03-manifest.md](03-manifest.md)) blocks *mix-and-match* — an
old object dropped into a slot whose record digest does not match — but it cannot block a
*consistent* replay of an older manifest together with its matching objects.

So: **git history and the remote are trusted for freshness, not only for authenticity.**
Rollback means the remote was compromised, and the answer is the compromise answer — migrate
to a new repo. The alternative, a per-machine signed monotonic counter, is out of scope for v1
and would not survive a remote that can serve any commit it likes.

## Distributing a new generation

| Situation | How the new key reaches the other Mac |
|---|---|
| **Hygiene rotation** — scheduled or routine, with **no known compromise and no machine being removed** | Publish `keys/<new_id>.wrap` in the same rotation commit: `KG_{n+1}` encrypted under `KG_n`, domain `key-wrap`, subject `be32(n+1)` ([04-envelope-v1.md](04-envelope-v1.md)). Any machine holding `KG_n` unwraps it after the next fetch. |
| **A machine is being removed** | **Out of band** (`key export` / `key import`). The machine being removed is by definition a holder of `KG_n`, so if it can still fetch, the wrap hands it the new key. Treat removal like a compromise. The only exception is a removal where the machine's access to the remote was revoked **before** the wrap was published — which is hard to establish after the fact, so do not rely on it. |
| **Key compromise** | **Never** wrap the new key under the leaked one — that leaks the new key too. Distribute out of band, or migrate to a new repo. |

The wrap is only as safe as the old key. Any machine that can still read `KG_n` can unwrap
`KG_{n+1}`, which is the same reasoning that forbids deriving a generation from its
predecessor above — and the same reason a removed device cannot be handed the wrap. After
unwrapping, verify the result against the published commitment for `n+1`
([above](#key-commitments-make-same-id-different-key-detectable)) before adding it to the ring.

**Order matters for out-of-band distribution.** Export and hand over the new generation only
**after** the rotation commit is pushed. A rotation that may still be discarded must never be
exported: a third machine would then adopt a generation the repo never accepted, and its
objects would fail authentication on every other machine.

## Open

- Whether `key prune` should also garbage-collect unreferenced `keys/*.wrap` files; the wrap
  is only useful while the generation it wraps under is live.
- The exact reachability walk (commit graph filter, or a per-commit manifest read) — deferred
  to the rotation slice, together with the re-encryption loop that #2 and #8 scope.
- Whether the ring should carry a monotonic "generation created" timestamp in the export blob
  for human diagnostics. It is not needed for correctness.