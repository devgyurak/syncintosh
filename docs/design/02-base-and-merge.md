# 02 — base, local, remote

**Status:** Accepted. Depends on [01-sync-model.md](01-sync-model.md).

## All three inputs are git objects

| Input | Where it comes from |
|---|---|
| `base` | the manifest and objects decrypted from `merge-base(HEAD, origin/<branch>)` |
| `local` | the commit made in step 5 of the cycle — the collect commit, **not the live files** |
| `remote` | `origin/<branch>` after fetch |

No separate plaintext "last synced" snapshot is stored. A second plaintext copy would fight
§3.3, and git already keeps the base as a commit. Deciding on commits rather than live files
also means the step-5 commit is the backup if anything later fails.

## base = ∅ — the first connect on a second Mac

When the second Mac is connected there is no common history, so `merge-base` finds nothing
and `base` is empty. **This is the primary product flow, not an edge case**: it is exactly the
"show the differences and let me pick" experience the README describes.

With `base = ∅` every record is either local-only or remote-only:

- Present on both sides with **identical canonical bytes** → one record, no prompt.
- Present on one side only → take it (a new file, no conflict).
- Present on both sides with different content → **conflict**, resolved per record as
  **adopt-local** or **adopt-remote**. "Keep both" is not offered for the same path; the
  user may instead drop the record.

Both sides are presented before anything is written, so the user sees the differences and
picks. Nothing is applied until the whole merge is conflict-free.

## Path collisions (two IDs for one file)

File IDs are random ([03-manifest.md](03-manifest.md)), so two machines that collected the
same path before ever sharing history generate **two different IDs for one path**. Keyed by
ID, that is not a conflict — the merge would happily produce two records for `~/.zshrc` and
`apply` would write the file twice.

The secondary unique key `(scope, machine, path)` catches it:

- Two records in the **merged result** sharing that key are a `PathCollision`, reported with
  the same adopt-local / adopt-remote choice.
- If their content is also identical, they are **deduplicated silently** — keep the
  lexicographically smaller ID, drop the other. This is deterministic, so both machines
  converge on the same manifest.
- The surviving record **keeps the ID of the side whose content won.** The loser's
  `objects/<id>.enc` is dropped. Because the loser was never in `base`, no tombstone is
  needed: on the other machine the same reasoning sees "absent from remote, unchanged from
  base → accept the deletion" and drops its copy on the next sync.

## machine-scope records

- A record with `scope = "machine"` is written and modified only by the machine named in its
  `machine` field. `apply` skips records whose machine is not this one.
- Every machine **carries all machine-scope records, including other machines'**, unchanged.
  Converging on the same manifest matters: if each Mac dropped the other's records, the two
  manifests would differ forever and every sync would rewrite `manifest.enc`.
- The secondary key includes `machine`, so two Macs tracking the same path machine-locally
  (Docker Desktop CPU/memory, a per-machine remote URL) do **not** collide.

## A stale base is normal

A Mac that was off for weeks has a very old merge-base. Nothing special happens: the same
record rules apply, and the only effect is more conflicts to resolve. There is no "base is
too old" refusal, because git still knows the true common ancestor.

## Divergence and retry

Two machines that both pushed leave a genuine merge-base commit, and the cycle's step 9
merges them. If the push loses a race:

- **Discard the unpushed merge commit** and redo steps 7–11 against the fresh
  `origin/<branch>`. The step-5 collect commit is kept.
- Never stack a second merge on top of an unpushed merge commit — that is what produces
  criss-cross history (two merge-bases), which sync then refuses
  ([01-sync-model.md](01-sync-model.md)).