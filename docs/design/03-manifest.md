# 03 — Manifest and identifiers

**Status:** Accepted. Depends on [01-sync-model.md](01-sync-model.md) and
[02-base-and-merge.md](02-base-and-merge.md).

The manifest is the encrypted index of the repo: which files are tracked, where they live,
and which format they are. It is stored as `manifest.enc` and it is the one object both
machines always touch, so its shape decides how clean a sync is.

## Shape

```json
{
  "schema": 2,
  "key_generation": 2,
  "policy": {
    "zsh": { "rev": 1, "digest": "<64 lowercase hex — see Policy stamp>" },
    "vscode": { "rev": 2, "digest": "<64 lowercase hex>" }
  },
  "records": {
    "<file_id>": {
      "path": ".zshrc",
      "preset": "zsh",
      "scope": "common",
      "format": "text",
      "sha256": "<64 lowercase hex — sha256 of the object's plaintext>"
    },
    "<file_id>": {
      "path": "Library/Application Support/Code/User/settings.json",
      "preset": "vscode",
      "scope": "machine",
      "machine": "<machine_id>",
      "format": "jsonc",
      "sha256": "<64 lowercase hex — sha256 of the object's plaintext>"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `schema` | Manifest schema version. A bump is a migration, not a routine change. |
| `key_generation` | The generation new objects are encrypted with ([05-key-generations.md](05-key-generations.md)). Readable only after decrypting the manifest; the manifest's *own* generation is in its envelope header ([04-envelope-v1.md](04-envelope-v1.md)). |
| `policy` | The preset policy the objects were made under, per built-in preset that has a record: `{preset: {rev, digest}}`. Always present in schema 2 (`{}` for a build with no catalog). See "Policy stamp". |
| `records` | Record map keyed by `file_id`. |
| `file_id` | 32 lowercase hex characters (`secrets.token_hex(16)`), fixed width. Generated **once** and reused forever. |
| `path` | The real path, **relative to `$HOME`** and without a leading `/`. Relative so the manifest is portable across the two Macs and does not bake in a user name. |
| `preset` | The preset that produced the record (provenance, and which noise keys apply). |
| `scope` | `"common"` or `"machine"` ([02-base-and-merge.md](02-base-and-merge.md)). |
| `machine` | Required when `scope = "machine"`; the machine ID from `~/.config/syncintosh/config.json`. Absent for common. |
| `format` | The adapter key: `text` · `jsonc` · `toml` · `yaml` · `xml` · `plist` · `binary`. |
| `sha256` | SHA-256 of the object's **plaintext**, 64 lowercase hex characters. It is what makes a content change visible to the record merge (see below). It leaks nothing — the manifest is encrypted. |

Paths, preset names, and scope live **only here**, never in a filename. The repo stores
`objects/<file_id>.enc` and nothing else that names the content (§3.2).

## Policy stamp

Two builds can disagree about what a preset strips: one has excluded a key the other still ships. Nothing in
an object says which policy produced it, so before this field every path that touched such an object had to
guess ([#24](https://github.com/devgyurak/syncintosh/issues/24)). The manifest now says so once, per preset.

* `digest` — the sha256 of the canonical JSON of the preset's **policy**: its `policy_epoch` (see below), then for each file its path, whether it is
  a glob, its format, its `noise` and `excluded` key paths, and its `ignore_list` flag; plus the preset's profile
  layout. File order and key order do not matter. Array rules, validators, bundle ID, download URL, and cask are
  **not** part of it — they change how a file is merged or where the app comes from, not which bytes an object
  holds. The user's ignore list itself is not part of it either: the user chooses it and it changes from cycle
  to cycle, so it is a carry (`core/ignore.py`), not a build property.
* `policy_epoch` — a number in the preset, raised by hand when the **code** that projects or carries the
  preset's files changes meaning (`core/projection.py`, `core/ignore.py`, a re-enabled strip). Object bytes then
  differ though no data field does; the epoch is how the digest sees it.
* `rev` — the position of that digest in the preset's append-only `POLICY_HISTORY`
  (`presets/history.py`), counted from 1. A policy change appends the new digest in the same commit; a golden
  test fails until the last entry equals the current digest.

Both are stored because neither is enough: a bare revision cannot tell our revision 3 from a fork's, and a bare
digest cannot say whether to migrate or to stop. `core/policy.py` compares them (`classify_policy`), against the
**local** history, per preset:

| The stamp names… | Verdict |
|---|---|
| a preset this build does not ship, or a `rev` past its history | `newer` |
| a `rev` whose recorded digest is not the stamp's | `unknown` |
| the latest revision | `match` |
| an earlier revision | `older` |

A preset with records but no stamp is `older`, whatever the length of its history: no stamp claims nothing, and
the objects may have been written by a build that did not project them (a schema-1 repo, or a build that did not
own the path). The migration is idempotent, so an object already projected keeps its bytes and only gains the
stamp. A record naming a preset that is not built in (a community preset) makes no claim.

A path's owner can change between builds. The stamp of the previous owner says nothing about the objects the new
owner now judges: the new owner is judged by its own stamp (or its absence, hence `older`), never by the earlier
owner's.

Only the **writer's own build** is ever stamped — a collect, a merge, and (by preserving what it found) a key
rotation. `key_generation` is independent: a rotation re-seals bytes, a policy change decides which bytes there
are, and a rotation neither re-derives nor blocks on the stamp.

### The gate

`services/policy_gate.py` reads the repo through the comparison, once per commit, so no path past it asks again:

* the **owner** of a record is found by its path (`PresetCatalog.preset_for`), never by the `preset` it carries, so
  a community name on a built-in path is judged — and stamped — as the built-in preset it really is;
* `newer` / `unknown` → `PolicyMismatch` ("upgrade syncintosh on this Mac"). The cycle stops before `collect` when
  HEAD is the offender, and right after the fetch — before any fast-forward or merge — when origin is. The step-5
  collect commit stays local and unpushed; nothing is merged, pushed, or applied. A CLI run exits 1, the daemon
  reports it and does nothing else, and `diff` prints a warning and continues read-only;
* `older` → each affected object is re-projected **in memory** under the local policy (the static strip only —
  a carry belongs to a live file, not to a stored object), re-sealed, and served in place of the stored one, with
  its record's digest, its format (the local one) and the stamp updated. HEAD, base, remote, target and restore
  all read the same view, so the merge, apply and ledger agree, and the next write persists the migration.
  A changed object passes the secret scan. The sync report says `policy migrated: <preset> rev a→b`.
  A record that cannot be reduced — its format is not the one the catalog uses, or the projection fails — does not
  stop the cycle: it is left out of the view (never served), its preset's stamp is not raised, and the cycle
  collects the live file in its place. The report says `policy not migrated: <path> …`. If this machine does not
  collect that path, the omission would read as a deletion and reach every other machine, so the cycle stops with
  `ProjectionError` instead.

A write is planned against the **stored** manifest, not the view: a migrated object would otherwise look unchanged
and never be written. A ledger entry's digest is of the object as it was stored when the entry was made, so a
read that no longer matches the view falls back to the stored object. `key rotate` reads and preserves the stamp
and is never blocked by it.

Stamping the local policy is honest only because a cycle brings every object it re-uses to that policy first
(migrate on `older`, stop on `newer`/`unknown`). A writer that stamped without that gate would turn a policy it
does not know into a `match`.

The stamp is a claim by whoever wrote the repo, so it is a signal for detection, not a trust boundary: `apply`
keeps its own projection check as defense in depth (`docs/design/02-base-and-merge.md`); it runs only when a live
file exists to restore into, and closing that gap is the follow-up PR.

## Schema versions

`schema` 2 added `policy`. A schema-1 manifest is still **read**, as a schema-2 manifest with no stamps, and the
next write persists it, so a repo upgrades by being synced: the manifest is rewritten once and no object is
(an object's bytes do not depend on the schema). A schema-1 build reading a schema-2 repo fails with
`UnsupportedManifestSchema`, which is the intended outcome: the older build does not know to stop on a policy
mismatch, so it must be upgraded first.

## Canonical serialization — an invariant

The manifest is serialized as **canonical JSON, UTF-8**:

- keys sorted by code point (`sort_keys=True`),
- no insignificant whitespace (`separators=(",", ":")`),
- `ensure_ascii=False`,
- values restricted to `str`, non-negative `int`, `bool`, `None`, `list`, and `dict` — **no
  floats** (nothing in the manifest is one, so the encoder rejects them),
- no trailing newline.

**The same logical manifest must always serialize to the same bytes, whatever order the
records were inserted in.** This is not cosmetic: the no-rewrite rule
([01-sync-model.md](01-sync-model.md)) compares plaintext bytes, so a non-canonical
serialization would rewrite `manifest.enc` on every sync even when nothing changed.

Pin it with a property test when the encoder lands (#2/#3): build the same manifest with the
record map inserted in different orders and assert the bytes are equal.

## Two merges, not one

A record carries metadata **and** the content digest, so there are two merges, in order:

1. **Record merge** (this section). Keyed by `file_id`, comparing canonical record bytes. It
   decides which records survive and which are in conflict.
2. **Content merge** (the adapter). For every surviving record whose content changed on at
   least one side, `services` selects a `ContentAdapter` by `record.format` and runs
   `parse -> merge_tree -> render` on the plaintext; the rendered text is re-parsed and compared
   with the merged tree, and a parse or round-trip failure is a conflict. A format with no
   structural adapter falls back to the line-level `MergeDriver` port, whose unreconcilable
   verdict is a whole-file conflict.

Splitting them is not tidiness. Because `sha256` is part of the record, a content-only change
*is* a record change — so a side that edits a file and a side that deletes it collide as
**delete/modify** and the edit is not silently dropped. Without the digest, the record would
be byte-identical on both sides ("unchanged") and the edit would vanish with no conflict.

But the record merge must **not** try to decide the content itself. It cannot tell "the other
side changed a key I did not touch" from "both sides changed the same key", so it routes every
content change into the content merge, and only the content merge may declare a content
conflict. That is where [`AGENTS.md` §2.9](../../AGENTS.md) applies: a structural adapter
(`adapters/jsonc.py`) turns the text into the key-path tree `core/merge.py` merges and renders
it back, and a format without one uses the line-level `MergeDriver` port instead.

## Record-level merge

Record merge is keyed by `file_id`. Two records are equal when their **canonical bytes** are
equal — which, the digest being one of those bytes, makes a content change a record change.
"Deleted" in the table below always means **absent from that side's manifest** (an explicit
`untrack`), never a missing live file.

| base | local | remote | result |
|---|---|---|---|
| absent | absent | absent | cannot occur |
| absent | present | absent | take local — a new file |
| absent | absent | present | take remote — a new file |
| absent | present | present | equal → take one; different → **conflict** |
| present | present | absent | local unchanged from base → delete; local changed → **delete/modify conflict** |
| present | absent | present | remote unchanged from base → delete; remote changed → **delete/modify conflict** |
| present | absent | absent | delete |
| present | present | present | equal to base → base; one side changed → that side; both changed identically → that side; both changed differently → **conflict** |

Then the secondary-key rule from [02-base-and-merge.md](02-base-and-merge.md) runs over the
merged result: two records sharing `(scope, machine, path)` are a `PathCollision`.

Every conflict is resolved by choosing a side. There is no automatic "both" for a single
path, and no automatic winner for a both-sides change. These are the **record** conflicts; a
record that survives them still has to pass the content merge, where the same key changed on
both sides is the adapter's conflict.

### What the digest buys

- **Cheap change detection.** Step 4 of the cycle ([01-sync-model.md](01-sync-model.md))
  compares a file's digest with its record instead of decrypting the object at `HEAD`.
- **Mix-and-match is refused.** An object replayed into a slot whose record digest does not
  match is detected before it is used. This is integrity against *substitution*; it is **not**
  freshness — a consistent replay of an older manifest and its objects is undetectable this
  way ([05-key-generations.md](05-key-generations.md)).
- **`apply` can verify what it writes** against the record digest.

### Deletion needs no tombstone

A deletion is *detected*, not *recorded*: a record present in `base` and absent on one side is
a deletion on that side. So no tombstone field exists, and a machine that was offline during
the deletion still converges — it sees the record in its own tree, absent from `remote`, and
itself unchanged from `base`.

**Deleting a record deletes `objects/<file_id>.enc` in the same commit.** A record without its
object, or an object without a record, is a corrupt repo; the manifest is the only index.

**A missing live file is not a deletion.** A record's file can be absent on a machine — the app
is not installed, so the apply is deferred, or the user deleted the file — and the record must
survive either way. Otherwise one Mac without the app would delete that setting on every
machine. Removing a record is an **explicit `untrack`**, and nothing else
([01-sync-model.md](01-sync-model.md)).

## Apply-path validation — the manifest is untrusted input

A record's `path` arrives from the repo, so it crosses a trust boundary: a compromised remote,
or a merged record from a machine the user does not control, could name a path outside the
configuration the app is allowed to touch. `apply` refuses a record unless **all** of these
hold:

- `path` is **relative**, has no `..` component, and has no NUL;
- the resolved target stays under `$HOME` — resolve the parent directory first, so a symlinked
  directory cannot be used to escape;
- `path` is one of the known paths of the preset named by the record's `preset`, according to
  the **local** preset catalog ([presets.md](presets.md));
- `path` is in this machine's **local allowlist** — an exact tracked item, or a directory **glob**
  the machine's own config names ([01-sync-model.md](01-sync-model.md), "Preset globs");
- the record's `scope` and `machine` say this machine may write it
  ([02-base-and-merge.md](02-base-and-merge.md)).

A record that fails any check is **not applied and is reported**, not silently skipped. Only
paths the local catalog already knows can be written, so a synced manifest cannot make the app
create a file in an arbitrary location.

The third check is implemented by `PresetCatalog.knows(preset, path)`: a record whose `preset`
names a **built-in** preset must name one of that preset's own paths, matched on the folded path.
A `preset` that is not built-in makes no claim to check, so it passes and the local allowlist
alone decides. The catalog consulted is always the **local** one, never a preset definition that
arrived with the record.

**The same check runs at collect**, on each of this machine's own items. Without it a config entry
naming a path its preset does not own (a hand-edited config, or a catalog that changed under it)
would publish a record that every machine's apply then refuses — including the one that published
it, so it would never get a ledger entry and every later sync would ask for `--adopt` again.
Collect refuses it and reports it, so nothing is published in the first place.

### Profile paths in the local allowlist

A profile file's record path is **logical** — `…/profiles/@<percent-encoded name>/<file>` — because the
real directory is a per-machine id. The manifest schema is unchanged, and a build that predates this
refuses the path at the catalog check (no preset owns it), which is the fail-closed direction. The local
allowlist adds a `profiles` entry (`{preset, scope, names: "*" | [names]}`, `core/config.LocalProfiles`):
a profile record is written only when this machine's config allows the **name**, and the name resolves
through this machine's own profile list. A record that carries a raw profile id is owned by no preset and
is refused. Every live read and write goes through one resolver (`services/profile_paths.py`); a logical
path with no real one — the profile is missing, the list is unreadable, a resource is borrowed — ends in a
deferral and is never read or written under its logical text. When the list cannot be read, **records are
kept** and the ledger baseline is left as it was: a live file that cannot be seen is not a deletion.
The real directory is re-checked (symlink, denied, outside `$HOME`) immediately before each write.

### Directory globs in the local allowlist

The local allowlist is the config's `items` (exact paths) plus its `globs` (directory patterns). A
glob is what lets a preset own a **directory** of per-machine files — VS Code's `snippets/*.json` —
which an exact item cannot express, because the file names differ between Macs. The pattern may only
sit in the last element (`**` and a pattern in a directory element are refused), so an expansion is
one listing and never a walk, and each expanded file runs the same deny → ownership → symlink →
projection checks an exact item does. A record is written under a glob **only because this machine's
own config names the same pattern** (`core/config.LocalGlob`), matched on the record's path and never
on its `preset` — which is what keeps a synced manifest from reaching a path the local config did not
choose.

### `~/.zshrc` that is a symlink

Decided with the first preset that needs it, `zsh`. An `apply` target that is a symbolic link is
**reported and never written**, exactly as on the collection side (§2.4). Following the link would
mean editing a file outside the tracked path; writing a regular file over it would silently break
a `~/.zshrc → ~/dotfiles/zshrc` setup. The link is surfaced to the user, who resolves it — the
same conservative posture as a first-sight file.

## Open

- Whether `schema` should be a per-record field as well as a manifest-wide one. Manifest-wide
  is enough today and simpler; per-record only helps if two record shapes ever coexist.
- Record `path` is relative to `$HOME`; a preset that writes outside `$HOME` (if one ever
  does) needs a second root or an absolute escape hatch. No such preset exists yet.
- A user-added path (Phase 4 "user item format inference") has no entry in the preset catalog,
  so the catalog check above would refuse it. That is deliberate: it needs an explicit local
  allowlist entry the user created, never one that arrived from the remote.