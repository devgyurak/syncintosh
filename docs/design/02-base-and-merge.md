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

**An identified array merges by element, not as one value.** A preset may declare an `ArrayRule`
(`core/keyed_array.py`) for an array inside a file. The rule names the identity fields of an element and
the group they fall in. The three-way decision is then made per group, with the same rule a key path gets:
a group one side changed takes that side; a group both sides changed to the same contents is taken once;
a group both sides changed differently is a conflict on that group alone. Local order is kept, and an
addition goes after its nearest predecessor on its own side. Both sides reordering the shared elements
differently is a whole-array conflict. An array the rule cannot read falls back to the whole-array compare.
Each conflicting group is one choice (its fingerprint covers that group's bindings only), and a proposed
value for a group must be an array whose every element belongs to that group.

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

Both are **key patterns**: a key path whose segments may be `ANY_KEY`, which matches exactly one object key
of any name (`context_servers.<name>.env`). It is a sentinel and not the string `*`, which is a legal key.
`ANY_KEY` is for `noise`/`excluded` only. The user's ignore list and a carried key stay exact paths, so a
setting called `*` is that setting, and `PresetFile.create` refuses an all-wildcard pattern and a wildcard on
an ignore-list file. The policy digest writes `ANY_KEY` as JSON `null`, so it cannot read as a key name.

A file may instead name an **allowlist** (`PresetFile.synced`, exact key paths): only the keys at or beneath
one of them travel, and every other key is local, cut and grafted back exactly as noise is. It is for an app whose
file mixes a few shared preferences with state and credentials and adds keys over time, where a deny-list would
carry each new key until someone noticed. `noise` and `excluded` still apply inside it, and `PresetFile.create`
refuses an empty allowlist, the root, a key that is also excluded (or beneath an excluded key), a non-keyed format,
and an ignore-list file. With no allowlist the policy digest is what it always was.

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
  step can resurrect it. A comment **inside** the excluded value (`"env": { // token`) goes with it, so it is not sealed and
  does not stop the projection. A comment outside the value is not uniform, for an exact path as much as for a
  pattern: one on the same line before the member is refused; one after the comma stays on the previous member's
  line; one between the key and the colon is removed; one on the line above the member stays in the sealed text,
  where the secret scan is the only defence (#83).

`restore_text` re-attaches *this machine's* values from the live file, ordered by the live file
so a stripped key returns to the position it had. Its result is projected again and required to
reproduce the repo text **byte for byte**: the apply ledger stores the projection's digest, so a
restore that did not round-trip would make the next cycle read the file as changed again — the
one failure that would loop forever.

**A user-named item the merge deleted.** A pattern with `ANY_KEY` names items the user created
(`context_servers.<name>`, `profiles.<name>`). When another machine deletes one, the local values left here
(an `env`, a noise path) would come back as an object with no `command`, or a profile holding only noise, and
Zed would be left with an invalid server. So `graft` drops them with the item. It decides from the two trees
alone, with no base, in one function (`_drops`) that `dropped_items` shares. A path's **items** are its prefixes cut
after each `ANY_KEY` of every pattern that matches it (`profiles.work`, and inside it
`profiles.work.settings.context_servers.gh`). An item is *deleted* when `live` has content under it that is neither
stripped nor carried (it was shared) **and** `repo` has nothing at or under it. A local value goes when **any**
enclosing item is deleted, so a deleted profile takes an env-only server inside it. An item that held only local
values (a server with just `env`, one with just `settings`, a profile with just noise) was never shared, so it
stays, and so does one inside a profile `repo` still has; an item `repo` still has stays even as `{}`. An exact path
has no item, so a fixed key such as `http.proxyAuthorization` is unchanged. This runs on apply only: the sealed
bytes, the policy digest and `policy_epoch` do not change, and the result is still required to project back to
`repo_text`.

The loss is **not recoverable**. An excluded value is in no commit, and the restore point holds only the projected
bytes (`01-sync-model.md`, "Commits and rewrites"), so it is not there either, whether the item vanished through a
merge or through `--adopt remote`. A sync that writes such a file therefore reports it in `SyncReport.dropped`
(the file, the item's label and the number of local keys it held, never a value), and the CLI prints it under
"removed with an item the synced settings do not have (not recoverable)". It is a notice and does not change the
exit code. The notice does not say which Mac deleted the item, because with no base a merge deletion and an item the repo never had (a first sync, `--adopt remote`)
cannot be told apart.

The cut is verified rather than trusted. `project_text` re-parses its own output and compares it
with the projected tree, so a renderer that half-applied an edit cannot seal a file that is
neither the original nor the projection. A path the local catalog cannot project — no adapter for
its format, a file that is not UTF-8, a value that cannot be rewritten without dropping a comment
— is **refused and reported**, never sealed raw ([03-manifest.md](03-manifest.md)).

### Carrying an ignored key

A key the **user** chose to keep local (VS Code's `settingsSync.ignoredSettings`) is not noise and is not
stripped. Stripping it would make the 3-way merge read it as *deleted* on this side and erase the value from
every other Mac. It is **carried**: its value in the sealed object comes from an object the repo already holds,
and `apply` puts this machine's own value back.

| Step | What it produces |
|---|---|
| `project_text(text, carry)` = `strip(carry(strip(parse(text)), source))` | the bytes sealed: the live file, with each carried key's value taken from `source` |
| `restore_text(repo, live, carry)` | the bytes `apply` writes: the repo object with this machine's value for every carried key (absent stays absent), verified by `project_text(restored, source=repo) == repo` |

- **The source is the apply ledger's object, not `HEAD`.** Change detection compares the live projection with the
  ledger, so a key carried from it never reads as a local edit, and a key that only the remote moved reads as
  `pending-apply` rather than as a change here. Carrying from `HEAD` would make the same remote-only change look
  like a local one and send an unchanged file through a stale-base merge every cycle. Where this machine has no
  ledger object, the object at `HEAD` is used; where there is none at all (a file not yet published), the carried
  keys are left out of the seal.
- **Strip, carry, strip.** The source is a stored object and may predate an exclusion, so the static strip runs
  again after the carry; an excluded key inside a carried value is never sealed.
- **The round trip is checked against the object being applied.** That object is the next cycle's ledger object,
  so it is the source the next projection will use.
- **A merge result is not carried again.** `_project_merged` applies the static strip only: the objects being
  merged already hold the carried values, and carrying again would put an older object's over them.
- **The text is copied, not re-written.** A carried value is taken from the source document span for span
  (`Verbatim`): `1e3` stays `1e3`, and a commented `"[python]": {...}` keeps its comments. Otherwise the byte-exact
  round trip above could not hold.
- **The list is read from the live file at the start of the cycle**, so a list that a merge changes takes effect on
  the next cycle: the key is still carried while that cycle applies, and synced from the one after.

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
- **A removed binding of a keyed array takes its notes with it.** The block of comment lines directly
  above it (no blank line between; a `//` line, or a `/* */` block even when it spans several lines),
  the comments on its own lines, and the comments inside it are removed with it. A blank line ends
  the block, so a section title separated from the binding stays. A rule-less array cannot tell which
  binding a note belongs to, so a removal that would take a note is refused with a fixed reason
  instead. (An earlier version kept the block above a removed binding; issue #21 reversed that.)
- **Two sides that reorder shared bindings differently are one conflict on the whole array.** The
  bindings that did not change decide the order, and no per-binding pick can settle it. The conflict
  says so, and that choosing a whole side drops the other side's added bindings.
- **Known behavior: a blank line beside an array bracket is cleaned up only by an edit that touches it.**
  A removal, move or replacement of the first binding takes the blank line after `[` with it, and the same
  edit to the last binding takes the blank line before `]`, so no blank line is left against a bracket. An
  insertion alone leaves them. An array with a rule rewrites a changed binding of the same group where it
  stands, so the blank line stays; an array without a rule handles a changed element as a deletion plus an
  insertion, so a blank line after `[` goes there too, as the one before `]` already did. A template that
  already had two blank lines after `[` keeps both when the first binding goes. Pinned by
  `test_a_blank_line_after_the_opening_bracket_goes_with_the_first_binding_an_edit_takes`,
  `test_a_blank_line_after_the_opening_bracket_that_no_edit_touches_stays` and
  `test_a_blank_line_after_the_opening_bracket_stays_exactly_when_the_first_binding_stays`; the rule-less
  case and its ruled contrast by
  `test_the_first_value_of_an_array_changing_keeps_or_drops_the_blank_line_after_the_bracket_by_its_rule`,
  and the two-blank-line case by `test_two_blank_lines_after_the_opening_bracket_both_stay_when_the_first_binding_goes`.
  The last two pin the behavior as it is, not as a recommendation.
- **Known limit: an array written with each comma at the start of its line is rewritten whole.** Element
  splicing needs a comma at the end of each element, so a comma-first array is replaced as one value, on
  one line: the merge result is right, but the user's blank lines and layout inside the array are lost.
  This predates issue #21. `test_a_comma_alone_on_its_line_is_not_spliced_and_stays_valid_json` pins that
  the result stays valid and merged; it does not pin the layout.
  An **object** has two such layouts, and they differ:
  - A comma **alone on a line** is rewritten whole only for an edit that works on that comma: a deleted
    member, the member a new one is inserted after, or the last survivor whose comma a tail deletion drops.
    When a comment is inside the object the render is refused with its own reason (`LONE_COMMA_REFUSED`)
    rather than dropping the comment (a conflict, not a broken file). Any other edit — a nested change, a
    value change, an add at the end or the head — is made in place and keeps the layout and the notes.
  - A comma at the **start of its line** (`, "b": 2`) is not spliced when a member is added or removed:
    that edit rewrites the object whole, and a comment inside it is refused with the general
    `VALUE_WITH_NOTE_REFUSED`. This predates issue #29. An edit that only changes a value below it is made
    in place (see the next item).
- **Known limit: an object in a mixed layout is edited in place only while its keys stay the same.** A
  first member on the `{` line, a closing brace after the last member, two members on one line, or a
  comment inside a one-line object is none of the layouts that are spliced member by member. When the tree
  has exactly the template's keys, each member's value is edited in its own span and the keys, separators,
  comments, and line breaks are untouched (issue #64), in the template's key order. Adding or removing a
  key in such an object still rewrites it whole, and is refused when a comment is inside it. Pinned by
  `test_a_value_change_in_a_mixed_layout_object_is_spliced_in_place` and
  `test_a_rewrite_that_would_drop_a_comment_is_refused`.

  This also changes the **sealed** bytes, not only the rendered ones. Where a strip or a carry touches a
  value below such an object, the projection is now spliced instead of rewritten whole
  (`test_project_text_keeps_a_mixed_layout_when_it_strips_a_nested_key`), so the stored object for that
  file differs from the one an older build made. **This is a deliberate exception to the rule that a change in
  what the code projects raises `policy_epoch`** (`03-manifest.md`, Policy stamp; `AGENTS.md` §3.2): the epoch
  is not raised and `POLICY_HISTORY` is not appended. The tree is the same, the shape is rare (a first member
  on the `{` line, or a `}` after the last member), and the cost is bounded to one file. Because the stamp is
  unchanged, an older build reads it as a match: **no gate stops it**, and the "newer stops the cycle" path
  does not apply. What was observed:
  - On the first sync after an upgrade, an unchanged live file of this shape projects to different bytes than
    the apply ledger holds (the ledger digest is of the projection), so it reads as a local change and is
    sealed once more with the same tree. The next sync is unchanged.
  - A machine that has not upgraded refuses to apply the new object for that one file: its restore does not
    reproduce the object byte for byte, so it stops at the round-trip check (`ProjectionError`, reported like
    any file that cannot be applied). The other files are unaffected.
  - Not tested: an older build that still has its own live file of this shape projects it to the old bytes
    and may seal them again, so a fleet running mixed builds could see this one file change back and forth
    until every machine is upgraded.

  A member-less object that carries a comment is not part of
  this limit: `{ /* note */ }` takes a new member after the comment (`{ /* note */ "x": 1 }`), and a block
  comment that shares the closing brace's line keeps the member out of the comment and moves `}` to its own
  line (issue #29). Pinned by `test_a_member_edit_lands_outside_a_comment_and_beside_a_lone_comma`.
- **A re-attached key comes back as the live file wrote it, unless a neighbouring line changed.** Projecting
  the live file is a list of text edits, and apply runs them backwards onto the repo text, so a stripped key
  returns where it stood, with its trailing comment, its one-line layout, and a nested value spelled the way
  the file had it. An edit is only moved when the line it sits on and the line before and after are the same
  in the repo text; the file's two ends count as lines. The result must also hold exactly the values the
  graft gives and project back to the repo object byte for byte, so this changes a layout and never a value.
  Known limit: when the other machine changed a line beside the key, or the checks fail, the key is
  re-attached through the renderer instead. The value is still the one this machine has, but the renderer
  places the key by its sibling, writes a nested value as the format's default, and drops a trailing comment.
  Pinned by `test_a_line_the_other_machine_changed_beside_the_key_falls_back_to_a_render_with_the_right_values`
  and, for the layouts that are kept, by `test_restoring_an_unchanged_projection_gives_back_the_live_file` and
  `test_a_line_the_other_machine_changed_far_away_comes_across_and_the_local_lines_stay`. A comment *above*
  a stripped key is not stripped with it: it stays in the sealed object, as it always did.

A strict consequence: **an object that is not itself a projection is refused, not applied.** Two builds that
disagree about a preset are told apart before this point, by the policy stamp (`03-manifest.md`, "The gate"): a
newer repo stops the cycle and an older one is migrated. What reaches apply should therefore be a projection or a
record the gate could not reduce, but the stamp is only a claim by whoever wrote the repo, so apply checks the
object itself. With a live file the round trip above must give the object back; with none, the object must be its
own static strip. Writing anything else would make the file's projection differ from the object, and the next
cycle would publish that difference, which the other build would publish back: a churn of commits instead of a
fix. The file is reported as `unprojectable` (and `sync` exits non-zero) so the user can see the disagreement and
act on it.