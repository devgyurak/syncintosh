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

The same choice recurs **per record** later on, whenever a record has no baseline on this
machine but its file already exists locally — the app was installed after the record was
synced, so a live file appears under a record this machine has never written — or when a
suppressed file comes back. It is resolved as adopt-local / adopt-remote, never by overwriting
([01-sync-model.md](01-sync-model.md)).

**The choice replaces the whole file and does not go through the content merge.** A first-sight
record has no usable `base`: with `base = ∅` every key would look changed on both sides and
conflict, and with a stale baseline the app's fresh defaults would win — the very outcome the
suppressed rule exists to prevent. So the chosen side *is* the result; no adapter 3-way runs
for it.

**Both sides keep the record's existing `file_id`.** adopt-remote keeps the record and only
permits the overwrite, and adopt-local replaces the content in place. Minting a new ID here
would put two IDs on one path and manufacture the very `PathCollision` described below. The
ledger then records the resulting content as a normal baseline, so the next cycle sees the file
as any other in-sync file ([01-sync-model.md](01-sync-model.md)).

## Path collisions (two IDs for one file)

File IDs are random ([03-manifest.md](03-manifest.md)), so two machines that collected the
same path before ever sharing history generate **two different IDs for one path**. Keyed by
ID, that is not a conflict — the merge would happily produce two records for `~/.zshrc` and
`apply` would write the file twice.

The secondary unique key `(scope, machine, path)` catches it:

- Two records in the **merged result** sharing that key are a `PathCollision`, reported with
  the same adopt-local / adopt-remote choice.
- A machine that has **never written** a record's file — no apply-ledger entry, so a deferred
  apply or a first connect — contributes its unapplied state, **not a deletion**
  ([01-sync-model.md](01-sync-model.md)). A missing file never removes a record.
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

Two machines that both pushed leave a genuine merge-base commit, and the cycle's merge step
(step 10) combines them. If the push loses a race:

- **Discard the unpushed merge commit** and redo steps 7–12 against the fresh
  `origin/<branch>`. The step-5 collect commit is kept.
- Never stack a second merge on top of an unpushed merge commit — that is what produces
  criss-cross history (two merge-bases), which sync then refuses
  ([01-sync-model.md](01-sync-model.md)).

## Projection

A preset may name keys that must never travel:

- **noise** — a machine- or session-specific value (window geometry, a chosen editor path). Left
  in, a change to it is a both-sides change and conflicts on *every* sync.
- **excluded** — a credential (`auths`, a proxy authorization header). This one must not reach
  the repo at all ([AGENTS.md](../../AGENTS.md) §3.4).

Both are removed **on the collect side**, before the object is sealed — not by the merge's
`ignore` set. Ignoring a key in `merge_tree` keeps it out of the *conflict* but still writes its
value into the object, which for `excluded` is exactly the leak. `services` projects every
tracked file a preset owns:

| Step | What it produces |
|---|---|
| `project_text(text)` = `render(strip(parse(text)), text)`, re-parsed and compared | the bytes that are hashed, staged, merged, and sealed |
| `restore_text(repo, live)` = `render(graft(parse(repo), parse(live)), repo)`, projected again | the bytes `apply` writes |

Two consequences follow, and they are the point of the whole mechanism:

- **A noise-only change is invisible.** The digest is the projection's, so the ledger, the
  record, and the write plan all see "unchanged": no diff, no conflict, and no commit.
- **An excluded key is in no object.** It was not in the bytes that were sealed, so no later
  step can resurrect it.

`restore_text` re-attaches *this machine's* values from the live file, ordered by the live file
so a stripped key returns to the position it had. Its result is projected again and required to
reproduce the repo text **byte for byte**: the apply ledger stores the projection's digest, so a
restore that did not round-trip would make the next cycle read the file as changed again — the
one failure that would loop forever.

The cut is verified rather than trusted. `project_text` re-parses its own output and compares it
with the projected tree, so a renderer that half-applied an edit cannot seal a file that is
neither the original nor the projection. A path the local catalog cannot project — no adapter for
its format, a file that is not UTF-8, a value that cannot be rewritten without dropping a comment
— is **refused and reported**, never sealed raw ([03-manifest.md](03-manifest.md)).

### What a render is allowed to change

The renderer edits the original text at the spans it must, so the layout of everything it did not
decide about survives:

- **A one-line object stays on one line.** A member is added or removed *in place* — appended
  before the closing brace with the separator its own removal takes back — because re-flowing the
  object to member-per-line would change the file far beyond what the merge decided, and then
  `project_text(restore(...))` could not reproduce the repo text at all. That byte-exactness is
  not cosmetic: a projection that did not round-trip would leave the file reported
  `unprojectable` on **every** later sync, so the machine would stop receiving the other
  machine's changes.
- **A comment above the first member stays with it.** A key inserted at the top of a multi-line
  object goes above that comment, not between the comment and the member it documents.
- **A re-attached *nested* value is re-rendered.** The value is the one this machine has, but it
  comes back from the tree rather than from the live file's own text, so a nested object's
  internal layout may differ from what the file had. A scalar value — the usual noise or excluded
  key — is byte-identical.

A strict consequence: **an object that is not itself a projection is refused, not applied.** An
object can hold a key the local policy strips when the machine that published it did not exclude
it — two builds disagreeing about the preset. Writing the file then would make its projection
differ from the object, and the next cycle would publish that difference, which the other build
would publish back: a churn of commits instead of a fix. The file is reported as `unprojectable`
(and `sync` exits non-zero) so the user can see the disagreement and act on it.