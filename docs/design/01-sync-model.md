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
| 4 | **stage** | Encrypt the records whose **live file changed against the apply ledger** — never against `HEAD` ([below](#change-detection-is-against-the-apply-ledger)). If nothing changed, write nothing and jump to step 6. |
| 5 | **commit** | Commit the staged ciphertext on the local branch. This is the backup commit §3.5 requires. |
| 6 | **fetch** | `git fetch` only. Never `git pull` — that would run a git-level merge. |
| 7 | **classify** | Compare `HEAD` with `origin/<branch>`. Equal → nothing to publish, go to 14. `HEAD` is an ancestor → **fast-forward** to the origin commit, go to 14. `origin` is an ancestor → push at 12 without any merge commit. Neither is an ancestor → a real divergence, continue 8–12. |
| 8 | **base** | `git merge-base HEAD origin/<branch>`; see [02-base-and-merge.md](02-base-and-merge.md). Divergence only. |
| 9 | **decrypt** | Materialize base, local, and remote into plaintext. Divergence only. |
| 10 | **merge** | The manifest record merge plus the per-object content merge ([03-manifest.md](03-manifest.md)). Divergence only. |
| 11 | **gate** | If a conflict is unresolved: write `~/.local/share/syncintosh/pending/`, leave the local commit in place, and **stop without pushing**. Divergence only. |
| 12 | **publish** | Push the step-5 commit when `origin` is an ancestor; otherwise re-encrypt only what changed and build the merge commit with **both parents** and a directly built tree (`git commit-tree`, never `git merge`), then push. |
| 13 | **retry** | On a non-fast-forward push, discard the unpushed merge commit, re-fetch, and redo 7–12. The step-5 commit is kept. |
| 14 | **apply** | Write the merged records into the live files — **after** the push and only after the per-file check below — then settle the apply ledger for every record ([below](#change-detection-is-against-the-apply-ledger)). |

**Apply runs after the push, never before.** The remote is the source of truth: if the push
fails, no live file has been touched and the step-5 commit still holds the collected state.

**Every write is checked (compare-and-swap).** Before overwriting a file, compare its current
bytes with that file's baseline: the bytes collected in step 2 when it was collected, or its
ledger digest when it was not (a pending apply). If they differ, the user edited the file
while the sync was running: abort the write, leave the file alone, and tell the user to
re-sync. Without this check a file edited between collect and apply is overwritten with the
pre-edit version — and that version is in no commit, so it cannot be recovered. The ledger is
left untouched on abort, so the next sync sees the edit as a local change against the pre-merge
baseline and merges it instead of reverting the remote.

**`apply` creates and overwrites conservatively.** It creates a file only when the ledger has
no entry for the record — this machine has never written it — and nothing is in the way; it
overwrites a file only when the ledger holds a normal (not suppressed) baseline. A file that
exists with no entry, or with a suppressed entry, is **first sight** and is left alone until
the user chooses (step 4 table), and a removed file is **locally suppressed** and is not
recreated. That is what stops a first connect from clobbering a file the user already had, and
stops a deliberate removal from being undone on every sync.

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

## Change detection is against the apply ledger

Step 4 must not compare the live files with `HEAD`. Apply runs *after* the push, so between
the push and the apply, `HEAD` already holds content this machine has not written yet. A
comparison against `HEAD` would then read the machine's own still-old files as "local edits",
collect them, and silently revert the other machine's change.

That window is not exotic. It is reached by **a crash before apply**, by a **compare-and-swap
abort**, and by a **deferred apply** when the app is not installed — three ordinary outcomes,
and the last one happens on every Mac that lacks the app.

So the machine keeps its own baseline, **outside git** (§3.3), one entry **per file**:

`~/.local/share/syncintosh/applied.json`

```json
{
  "files": {
    "<file_id>": {
      "sha256": "<digest of the live content this machine has accounted for>",
      "commit": "<the commit in which that content entered history>",
      "suppressed": false
    }
  }
}
```

**Ledger invariant.** After a cycle settles, `files[F]` exists exactly when this machine has a
baseline for `F`: either the live file matches the content of the published `HEAD`, or the
entry is marked `suppressed`. For a normal baseline, `commit` is where that content entered
history; a suppressed entry keeps the digest it had when the file disappeared, for
diagnostics.

**Suppression is a stored flag, not an inference from the file's absence.** The file can come
back — the app is uninstalled and reinstalled, and it writes a fresh default — and an inferred
flag would disappear at that exact moment, turning the app's new default into a "local change"
that then outweighs the other machines' settings in the content merge, with no conflict. The
flag is what prevents that; see the table below.

- `apply` writes an entry as each file lands.
- `init` / `add` seed the ledger by adopting the live files as this machine's baseline.
- **Publishing settles it too.** A file the user changed locally is collected, committed, and
  pushed, and `apply` then writes nothing — so apply alone would leave the old digest behind.
  The next local change would then look like a change against a long-stale base, and the user
  would be handed a conflict over an edit they had already published. So at the end of every
  successful cycle, every record whose live digest equals the digest in the published commit
  has its entry (re)written as a **normal baseline** — `{live digest, published commit,
  suppressed: false}` — which is also what clears a suppression once the file is back in
  agreement with the repo.

Per record, step 4 decides:

| Ledger | Live file | `HEAD` record | Classification |
|---|---|---|---|
| absent | missing | present | **Deferred.** Never applied here and nothing to adopt (typically the app is not installed, so the directory does not exist). Do not collect; report. |
| absent | present | present | **First sight** — the app was installed after the record was synced, or this is a first connect. Do not overwrite and do not collect: route it to the `base = ∅` choice (adopt-local / adopt-remote, [02-base-and-merge.md](02-base-and-merge.md)). If the live digest already equals the record's, it is simply in sync. |
| present, not suppressed | missing | any | **Locally suppressed.** The file is gone (the app was uninstalled, or a deliberate removal). Set `suppressed = true`; do **not** collect it and do **not** recreate it; report it, and recreate only on an explicit command. Recreating it on every sync is what would make a deliberate removal impossible. |
| `suppressed: true` | missing | any | Still suppressed. Nothing to collect and nothing to write; report it. |
| `suppressed: true` | present | any | **First sight — the file came back.** A reinstalled app writes a fresh default, which is not an edit of the old baseline; collecting it would let those defaults win the content merge against the other machines' settings with no conflict. Do not collect: route it to the `base = ∅` choice, and clear the flag only once the live digest equals the published digest. |
| present, not suppressed | equals the ledger | equals the ledger | in sync |
| present, not suppressed | equals the ledger | differs | **Pending apply** — `HEAD` holds content this machine has not written. Exclude from collect and finish the apply. |
| present, not suppressed | differs | any | **Local change.** The `base` for its content merge is the object for `F` at `files[F].commit` — a stale base is ordinary (§5.7), not an error. |

Two consequences:

- **A missing file is never a deletion.** It means "not applied here" — the app is not
  installed, or the user removed it — not "delete the record". The record stays in the
  manifest and the other machines keep their copy. Only an explicit `untrack` removes a record
  ([03-manifest.md](03-manifest.md)).
- A record this machine has never written has **no baseline**, so it can never be a
  *modification* in the record merge, and it neither creates nor resolves a `PathCollision`
  beyond its manifest metadata. If its file happens to exist, that is a **first-sight** choice
  for the user, not a merge input ([02-base-and-merge.md](02-base-and-merge.md)).

## The scan is not a step that can be skipped

The scan is enforced at the **encrypt boundary** in `services/`: nothing reaches
`crypto.encrypt` without passing it. That is what makes §3.4's "before every encrypt" true for
the merge re-encrypt in step 12, for `resolve`, and for a change the LLM proposed — not only
for the initial collect. A design that scans once at step 3 and then encrypts merge output at
step 12 would violate the rule while looking compliant.

### The scan's ambiguous shapes, and the local way out

The scan is a heuristic, and a few shapes are genuinely ambiguous — RFC 3986 allows `,` and `;` in
a URL's userinfo, and the same bytes separate list entries — so it **fails closed** there. A user
with one legitimate line gets no way out but to edit the file, so a **machine-local, never-synced**
allowlist exists (`~/.local/share/syncintosh/scan-allow.json`, mode 0600). An entry names a
`(path, pattern label, fingerprint)`, never raw text: the fingerprint is an HMAC of the matched
span under a random per-machine key, so the file stores no plaintext and a plain hash's
short-password weakness does not apply. A refusal prints the fingerprint, and `syncintosh scan
allow` records it. It stays outside the repo on purpose — a synced allowlist would let one machine
tell another what not to scan (``AGENTS.md`` §3.3).

## Commits and rewrites

- **No change means no commit.** If the plaintext set did not change, step 4 writes nothing
  and step 5 commits nothing.
- **The backup commit is the HEAD before apply** — the step-5 collect commit — **not a fresh
  commit on every sync.** This is what §3.5 means; it avoids empty-commit churn.
- **An object whose plaintext did not change is not rewritten.** A random nonce (§3.1) means
  rewriting an object always changes its bytes, so an unconditional write would rewrite
  every ciphertext on every sync and keep git history permanently dirty. Changed-ness is
  decided on **plaintext**, by comparing the merged plaintext with the record's `sha256`
  ([03-manifest.md](03-manifest.md)) — with no need to decrypt the object at `HEAD`.
- The manifest follows the same rule, which is why its serialization must be canonical
  ([03-manifest.md](03-manifest.md)). Without that, an unchanged manifest would re-serialize
  to different bytes and rewrite `manifest.enc` every sync.

## Repository shape

```
objects/<file_id>.enc     one ciphertext per tracked file
manifest.enc              the encrypted manifest
keys/<generation>.wrap    a new generation key wrapped under the previous one
keys/commitments.json     generation id -> key commitment, cleartext ([05-key-generations.md](05-key-generations.md))
.gitattributes
```

`.gitattributes` marks the stored objects as binary so a human running `git pull` or
`git merge` by hand cannot mangle them:

```
objects/*.enc         binary -diff -merge
manifest.enc          binary -diff -merge
keys/*.wrap           binary -diff -merge
keys/commitments.json binary -diff -merge
```

This is **defense in depth, not the primary mechanism**: syncintosh builds the merge tree
with `git commit-tree`, so `git merge` is never in the sync path.

## vcs requirements

- **No shallow clone or fetch.** `merge-base` needs history, so `vcs/` must never pass
  `--depth` to `clone` or `fetch`.
- **`fetch` + `commit-tree` + `push` only**; `pull`, `merge`, and `rebase` are never
  invoked.
- Push failure is classified as non-fast-forward (retry per step 13) versus anything else
  (report).