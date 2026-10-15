# 01 — The sync model

**Status:** Accepted. Independent; notes 02–05 assume it.

## The one-line rule

**Git never merges ciphertext, and sync never calls `git merge`.**

Ciphertext is opaque: two machines that changed the same file produce two unrelated blobs, so
a git-level merge would be a conflict on every overlapping object and on the manifest (which
is a single blob, so *any* change collides). Instead sync decrypts both sides, merges the
**plaintext**, and builds the resulting commit's tree itself.

## The cycle

| # | Step | Notes |
|---|---|---|
| 1 | **lock** | Take the advisory lock at `~/.local/share/syncintosh/sync.lock`. **Every** mutating operation takes it — `sync`, `resolve`, `rollback`, and `key rotate` — so a rotation cannot race a sync. The daemon and the CLI share it; a second caller waits for a bounded time, then reports "another sync is running" instead of racing. Read-only commands (`status`, `diff`) do not take it. |
| 2 | **collect** | Read the preset paths into the plaintext mirror as **regular files, never symlinks** (§2.4). Dangerous paths are refused here, before anything is read (§3.4). |
| 3 | **scan** | The secret scan runs over the collected plaintext (§3.4). It is part of the **encrypt boundary**, not a step that can be skipped — see below. |
| 4 | **stage** | Encrypt the objects whose plaintext changed. Changed-ness is decided by comparing the file's digest with the record's `sha256` ([03-manifest.md](03-manifest.md)), **not** by decrypting `HEAD`. If nothing changed, write nothing and jump to step 6. |
| 5 | **commit** | Commit the staged ciphertext on the local branch. This is the backup commit §3.5 requires. |
| 6 | **fetch** | `git fetch` only. Never `git pull` — that would run a git-level merge. |
| 7 | **classify** | Compare `HEAD` with `origin/<branch>`. Equal → nothing to publish, go to 14. `HEAD` is an ancestor → **fast-forward** to the origin commit, go to 14. `origin` is an ancestor → push at 12 without any merge commit. Neither is an ancestor → a real divergence, continue 8–12. |
| 8 | **base** | `git merge-base HEAD origin/<branch>`; see [02-base-and-merge.md](02-base-and-merge.md). Divergence only. |
| 9 | **decrypt** | Materialize base, local, and remote into plaintext. Divergence only. |
| 10 | **merge** | The manifest record merge plus the per-object content merge ([03-manifest.md](03-manifest.md)). Divergence only. |
| 11 | **gate** | If a conflict is unresolved: write `~/.local/share/syncintosh/pending/`, leave the local commit in place, and **stop without pushing**. Divergence only. |
| 12 | **publish** | Push the step-5 commit when `origin` is an ancestor; otherwise re-encrypt only what changed and build the merge commit with **both parents** and a directly built tree (`git commit-tree`, never `git merge`), then push. |
| 13 | **retry** | On a non-fast-forward push, discard the unpushed merge commit, re-fetch, and redo 7–12. The step-5 commit is kept. |
| 14 | **apply** | Write the merged records into the live files — **after** the push, and only after the per-file check below. |

**Apply runs after the push, never before.** The remote is the source of truth: if the push
fails, no live file has been touched and the step-5 commit still holds the collected state.

**Every write is checked (compare-and-swap).** Before overwriting a file, compare its current
bytes with the bytes collected in step 2. If they differ, the user edited that file while the
sync was running: abort the write, leave the file alone, and tell the user to re-sync. Without
this check a file edited between collect and apply is overwritten with the pre-edit version —
and that version is in no commit, so it cannot be recovered.

`apply` also skips records belonging to another machine
([02-base-and-merge.md](02-base-and-merge.md)), and defers a record whose app is not
installed, reporting the download URL or brew command instead of writing into a directory that
does not exist.

**Fast-forward is not a merge.** An ancestor relationship is resolved by moving or pushing,
never by building a commit with two parents. Merging when one side is already an ancestor is
harmless for correctness but adds an empty commit on every sync: the machines then alternate
`M(X, Y)` and `N(Y, M)` forever, which contradicts "no change means no commit".

**Conflicts never touch live files.** `apply` runs only on a conflict-free merge, so a failed
sync leaves the user's files exactly as they were; the pending state records the merge inputs
and the unresolved records.

While a pending conflict exists, `sync` refuses to start a new merge (it reports the pending
conflict and exits); the daemon treats that as a no-op and surfaces it. Only `resolve` (and
`rollback`) may run. **`resolve` re-runs the cycle from step 2**, so edits made after the
failed sync become part of the new local side instead of being overwritten; step 14's check
guards the write regardless.

**Criss-cross history is a hard error.** If `git merge-base --all` returns more than one
commit, sync stops and reports it rather than inventing a recursive merge.

## The scan is not a step that can be skipped

The scan is enforced at the **encrypt boundary** in `services/`: nothing reaches
`crypto.encrypt` without passing it. That is what makes §3.4's "before every encrypt" true for
the merge re-encrypt in step 12, for `resolve`, and for a change the LLM proposed — not only
for the initial collect. A design that scans once at step 3 and then encrypts merge output at
step 12 would violate the rule while looking compliant.

## Commits and rewrites

- **No change means no commit.** If the plaintext set did not change, step 4 writes nothing
  and step 5 commits nothing.
- **The backup commit is the HEAD before apply** — the step-5 collect commit — **not a fresh
  commit on every sync.** This is what §3.5 means; it avoids empty-commit churn.
- **An object whose plaintext did not change is not rewritten.** A random nonce (§3.1) means
  rewriting an object always changes its bytes, so an unconditional write would rewrite
  every ciphertext on every sync and keep git history permanently dirty. Changed-ness is
  decided on **plaintext** — the file's digest against the record's `sha256`
  ([03-manifest.md](03-manifest.md)) — with no need to decrypt the object at `HEAD`.
- The manifest follows the same rule, which is why its serialization must be canonical
  ([03-manifest.md](03-manifest.md)). Without that, an unchanged manifest would re-serialize
  to different bytes and rewrite `manifest.enc` every sync.

## Repository shape

```
objects/<file_id>.enc     one ciphertext per tracked file
manifest.enc              the encrypted manifest
keys/<generation>.wrap    a new generation key wrapped under the previous one
.gitattributes
```

`.gitattributes` marks the ciphertext as binary so a human running `git pull` or `git merge`
by hand cannot mangle it:

```
objects/*.enc binary -diff -merge
manifest.enc  binary -diff -merge
keys/*.wrap   binary -diff -merge
```

This is **defense in depth, not the primary mechanism**: syncintosh builds the merge tree
with `git commit-tree`, so `git merge` is never in the sync path.

## vcs requirements

- **No shallow clone or fetch.** `merge-base` needs history, so `vcs/` must never pass
  `--depth` to `clone` or `fetch`.
- **`fetch` + `commit-tree` + `push` only**; `pull`, `merge`, and `rebase` are never
  invoked.
- Push failure is classified as non-fast-forward (retry per step 12) versus anything else
  (report).