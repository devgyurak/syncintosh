# 03 — Manifest and identifiers

**Status:** Accepted. Depends on [01-sync-model.md](01-sync-model.md) and
[02-base-and-merge.md](02-base-and-merge.md).

The manifest is the encrypted index of the repo: which files are tracked, where they live,
and which format they are. It is stored as `manifest.enc` and it is the one object both
machines always touch, so its shape decides how clean a sync is.

## Shape

```json
{
  "schema": 1,
  "key_generation": 2,
  "records": {
    "<file_id>": {
      "path": ".zshrc",
      "preset": "zsh",
      "scope": "common",
      "format": "text"
    },
    "<file_id>": {
      "path": "Library/Application Support/Code/User/settings.json",
      "preset": "vscode",
      "scope": "machine",
      "machine": "<machine_id>",
      "format": "jsonc"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `schema` | Manifest schema version. A bump is a migration, not a routine change. |
| `key_generation` | The generation new objects are encrypted with ([05-key-generations.md](05-key-generations.md)). Readable only after decrypting the manifest; the manifest's *own* generation is in its envelope header ([04-envelope-v1.md](04-envelope-v1.md)). |
| `records` | Record map keyed by `file_id`. |
| `file_id` | 32 lowercase hex characters (`secrets.token_hex(16)`), fixed width. Generated **once** and reused forever. |
| `path` | The real path, **relative to `$HOME`** and without a leading `/`. Relative so the manifest is portable across the two Macs and does not bake in a user name. |
| `preset` | The preset that produced the record (provenance, and which noise keys apply). |
| `scope` | `"common"` or `"machine"` ([02-base-and-merge.md](02-base-and-merge.md)). |
| `machine` | Required when `scope = "machine"`; the machine ID from `~/.config/syncintosh/config.json`. Absent for common. |
| `format` | The adapter key: `text` · `jsonc` · `toml` · `yaml` · `xml` · `plist` · `binary`. |

Paths, preset names, and scope live **only here**, never in a filename. The repo stores
`objects/<file_id>.enc` and nothing else that names the content (§3.2).

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

## Record-level merge

Merge is keyed by `file_id`. Two records are equal when their **canonical bytes** are equal.

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
path, and no automatic winner for a both-sides change.

### Deletion needs no tombstone

A deletion is *detected*, not *recorded*: a record present in `base` and absent on one side is
a deletion on that side. So no tombstone field exists, and a machine that was offline during
the deletion still converges — it sees the record in its own tree, absent from `remote`, and
itself unchanged from `base`.

**Deleting a record deletes `objects/<file_id>.enc` in the same commit.** A record without its
object, or an object without a record, is a corrupt repo; the manifest is the only index.

## Open

- Whether `schema` should be a per-record field as well as a manifest-wide one. Manifest-wide
  is enough today and simpler; per-record only helps if two record shapes ever coexist.
- Record `path` is relative to `$HOME`; a preset that writes outside `$HOME` (if one ever
  does) needs a second root or an absolute escape hatch. No such preset exists yet.