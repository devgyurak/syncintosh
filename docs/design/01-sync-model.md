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
| 1 | **lock** | Take the advisory lock at `~/.local/share/syncintosh/sync.lock`. The daemon and the CLI share it; a second caller waits for a bounded time, then reports "another sync is running" instead of racing. |
| 2 | **collect** | Read the preset paths into the plaintext mirror as **regular files, never symlinks** (§2.4). Dangerous paths are refused here, before anything is read (§3.4). |
| 3 | **scan** | The secret scan runs over the collected plaintext. It runs before **every** encrypt, with no exception (§3.4). |
| 4 | **stage** | Encrypt the objects whose plaintext changed. If the plaintext set is byte-identical to `HEAD`, write nothing and jump to step 6. |
| 5 | **commit** | Commit the staged ciphertext on the local branch. This is the backup commit §3.5 requires. |
| 6 | **fetch** | `git fetch` only. Never `git pull` — that would run a git-level merge. |
| 7 | **base** | `git merge-base HEAD origin/<branch>`; see [02-base-and-merge.md](02-base-and-merge.md). |
| 8 | **decrypt** | Materialize base, local, and remote into plaintext. |
| 9 | **merge** | App-level, record-level manifest merge plus a per-object content merge ([03-manifest.md](03-manifest.md)). |
| 10 | **gate** | If a conflict is unresolved: write `~/.local/share/syncintosh/pending/`, leave the local commit in place, and **stop without pushing**. |
| 11 | **publish** | Re-encrypt only what changed, build the merge commit with **both parents** and a directly built tree (`git commit-tree`, never `git merge`), and push. |
| 12 | **retry** | On a non-fast-forward push, discard the unpushed merge commit, re-fetch, and redo 7–11. The step-5 commit is kept. |

**Conflicts never touch live files.** `apply` runs only on a conflict-free merge, so a failed
sync leaves the user's files exactly as they were; the pending state records what was merged
and what is unresolved so `resolve` can finish without re-collecting.

While a pending conflict exists, `sync` refuses to start a new merge (it reports the pending
conflict and exits); the daemon treats that as a no-op and surfaces it. Only `resolve` (and
`rollback`) may run.

**Criss-cross history is a hard error.** If `git merge-base --all` returns more than one
commit, sync stops and reports it rather than inventing a recursive merge.

## Commits and rewrites

- **No change means no commit.** If the plaintext set did not change, step 4 writes nothing
  and step 5 commits nothing.
- **The backup commit is the HEAD before apply** — the step-5 collect commit — **not a fresh
  commit on every sync.** This is what §3.5 means; it avoids empty-commit churn.
- **An object whose plaintext did not change is not rewritten.** A random nonce (§3.1) means
  rewriting an object always changes its bytes, so any unconditional write would rewrite
  every ciphertext on every sync and keep git history permanently dirty. Changed-ness is
  decided on **plaintext**: decrypt the object at `HEAD` and compare with the newly
  serialized plaintext. A missing object at `HEAD` counts as changed.
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