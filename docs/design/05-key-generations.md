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
    def drop(self, key_id: int) -> None: ...  # explicit prune only
```

**This shape is fixed now** even though the port change lands in #2, because it is expensive
to change once objects are stored: a single-key `get()` cannot resolve a `key_id`, and
retro-fitting one means rewriting every stored object. (The port that exists today is the
single-key `get()`/`set()` pair.)

`get()` returning `None` for a generation this machine does not hold is what makes
`UnknownKeyGeneration` say *"import it"* rather than *"unrecoverable"*
([04-envelope-v1.md](04-envelope-v1.md)).

**`KG_1` is the master key.** `init` creates it, the recovery-key backup protects it, and
§3.1's "wrapped with a KDF for export/import" applies to it. `key export` wraps the **whole
ring** — `KG_1` first, then every retained generation — with scrypt or Argon2id, so exporting
and importing the ring is one operation and "lose the master key" and "lose the ring" are the
same failure. The KDF parameters are never reduced for tests (§3.1).

Late generations are **independent random keys**, never derived from an earlier generation. If
`KG_{n+1}` were `HKDF(KG_n, …)`, then anyone holding `KG_n` could derive `KG_{n+1}`, and a
rotation after a device was removed would protect nothing.

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

## Threat model — one line that matters

**Rotation protects future data only.** Ciphertext already in git history stays decryptable
with the old key, so rotation is the right answer to "a device was removed" and the wrong
answer to "the key leaked".

**On compromise, migrate to a new repo.** Force-pushing to drop old commits is not an option:
§7 denies force-push, and the merge-base model needs shared history
([01-sync-model.md](01-sync-model.md)). A fresh repo with a fresh generation 1 is the
supported answer.

## Distributing a new generation

| Situation | How the new key reaches the other Mac |
|---|---|
| **Routine rotation** (device removed, key not leaked) | Publish `keys/<new_id>.wrap` in the same rotation commit: `KG_{n+1}` encrypted under `KG_n`, domain `key-wrap`, subject `be32(n+1)` ([04-envelope-v1.md](04-envelope-v1.md)). Any machine holding `KG_n` unwraps it after the next fetch. |
| **Compromise rotation** (the old key leaked) | **Never** wrap the new key under the leaked one — that leaks the new key too. Distribute out of band with `key export` / `key import`, or migrate to a new repo. |

## Open

- Whether `key prune` should also garbage-collect unreferenced `keys/*.wrap` files; the wrap
  is only useful while the generation it wraps under is live.
- The exact reachability walk (commit graph filter, or a per-commit manifest read) — deferred
  to the rotation slice, together with the re-encryption loop that #2 and #8 scope.
- Whether the ring should carry a monotonic "generation created" timestamp in the export blob
  for human diagnostics. It is not needed for correctness.