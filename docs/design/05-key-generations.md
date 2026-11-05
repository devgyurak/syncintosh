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

An entry that is **present but unreadable** — not valid hex, or not 32 bytes — is a different
failure: `KeyStoreCorrupt` (a `CryptoError`, in `core` alongside the others). The generation
exists; it is corrupt; the fix is to re-import it, not to report the ciphertext as tampered.
`put` cannot repair one on its own — it reads the entry first, to detect a second key, so a
corrupt entry raises there too. Whether an import may **overwrite** a corrupt entry (repair)
rather than stop is part of the import/rotation slice (#8).

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
- The passphrase is **normalized to Unicode NFC** before UTF-8 encoding. One text typed in
  NFC and pasted from an NFD source (some file names and note apps) must derive one key; the
  export format is a contract, so this can never be loosened later.
- An export must use parameters this build would also **unwrap**: `wrap_ring` refuses
  parameters outside the same allowlist, so a backup that cannot be opened is never written.
- **Failure is one error.** A wrong passphrase and a damaged blob both surface as
  `MasterKeyUnwrapError` ([04-envelope-v1.md](04-envelope-v1.md)) — the user retries the
  passphrase either way.

## Retention

- **Generations are retained by default.** There is no automatic expiry.
- **Prune is explicit** (`key prune <generation>`) and must refuse while the generation is
  still needed. *Needed* means reachable: some commit reachable from this machine's `HEAD` or
  from `origin/<branch>` has a tree holding an envelope encrypted under it. Generation 1 — the
  master generation, without which the ring cannot be exported — is refused outright.
- **The reachability walk reads no key.** `key_id` is cleartext in the envelope header
  ([04-envelope-v1.md](04-envelope-v1.md)), so `git rev-list --objects <HEAD> <origin/branch>`
  lists every reachable blob, the `manifest.enc` and `objects/<id>.enc` slots are kept, and one
  `cat-file --batch` reads each 5-byte header, one object at a time, skipping the body in
  bounded chunks so memory stays flat on a large history. The header parse lives in
  `core/envelope_header.py`, the single definition the crypto layer also builds from, so
  `vcs` needs no `crypto` import. Git prints one path per unique blob; because every
  envelope uses a fresh random nonce, one ciphertext lives at exactly one path, so the listing
  reports the slot path. `keys/*.wrap` is deliberately **not** walked: a wrap's header names the
  generation it is wrapped *under*, not a stored object's generation.
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
- When the local branch is ahead of the remote at a higher generation, `key rotate` adopts the remote
  tip and discards the unpushed rotation **together with any collect commit stacked on it**. That loses
  no edit: a collect commit contains only what the live files already say, the apply ledger is saved
  only after an apply, so the next `sync` still sees every edit as changed and collects it again on the
  adopted generation (`tests/integration/test_sync_two_machines.py` pins this). Refusing instead would
  strand the machine, because `sync` cannot publish while the ring holds a generation the remote does
  not publish (a pending-push marker lets `sync` settle that case itself, below).
- **A rotation of unknown outcome leaves a pending-push marker.** `key rotate` records `{generation, commit}`
  in `pending-push.json` (state directory, outside git, no key) inside its discard guard just before the push,
  and clears it when the push succeeds, when a rejected push turns out to have landed, or when the rotation is
  proven absent and discarded. An unknown outcome keeps both the generation and the marker. A later `key rotate`
  clears the stale marker when it sweeps the stray generation and records its own.
- **`sync` repairs an unpublished rotation only with that marker, and only when it is hygiene.** If the remote
  publishes the marked generation under this machine's key, the push had landed and the marker is cleared.
  Otherwise, when the marker names exactly the local generation, its commit is in the local history, the remote
  does not publish the generation, and the local branch is not behind the remote (it **diverged** from it or is
  **ahead** of it), the outcome depends on the mode. Hygiene (its `keys/<n>.wrap` is in the local tree): `sync`
  adopts the remote tip, removes the stray generation and its wrap from the ring (the same sweep as `key rotate`,
  for the same reason as above), clears the marker, restarts the cycle, and notes that the rotation was discarded
  and can be repeated with `syncintosh key rotate --hygiene`. Dropping any collect commits stacked on it loses
  no edit: the apply ledger is saved only after an apply, so the next `sync` still sees every edit as changed and
  collects it again on the adopted generation. Out-of-band (no wrap): never discarded by a daemon, since it
  may be the removal of a device or the answer to a leaked key and undoing it would leave that access in place;
  `sync` stops and asks for `syncintosh key rotate --out-of-band` again.
- **The marker is what makes this safe against a hostile remote.** The commitments file is unauthenticated input:
  a remote that omits or forges an entry, or is rewound past a rotation, must not be able to talk a machine out of
  a key it published. Without a marker a held generation the remote does not publish is still
  `CommitmentsCorrupt`, as before, and a generation the remote does publish is judged by the normal checks.
  Every other held generation must be published, so the sweep can only drop the marked one. **Residual window:**
  while a marker exists for a rotation that in fact landed (a lost response), a remote rewound past that
  rotation can still make `sync` drop the key until a `sync` sees the generation published and clears the marker.
  The commit and the wrap (under the retained previous generation) stay in the clone's object store until
  `git gc`, and another machine or a ring export still holds the key; the report names the discarded generation.
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
- Whether the ring should carry a monotonic "generation created" timestamp in the export blob
  for human diagnostics. It is not needed for correctness.