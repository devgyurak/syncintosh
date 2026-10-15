---
name: add-adapter
description: Add or change a syncintosh format adapter (jsonc, toml, yaml, xml, plist, text, binary). Use on "새 포맷 지원" (support a new format), "YAML 어댑터 붙여줘", or "이 파일도 merge 되게 해줘". Enforces comment/order-preserving round-trip, noise removal, and key-level conflict rules.
---

# Add or change a format adapter

An adapter turns a settings file into a **key-level** structure the merge engine can reason
about, and turns a merged structure back into a file. Line-level git merge breaks JSON/XML
syntax, which is why this layer exists.

## Contract

Every adapter implements the `Adapter` port from `core/ports.py`:

- `parse(text) -> Document` — to a comparable structure (keys and values, ordered).
- `render(document, original) -> str` — back to text.
- `is_conflict(base, local, remote, key) -> bool` — **only a both-sides change on the same
  key is a conflict** (AGENTS.md §5.3).

The port lives in `core/`; the adapter lives in `adapters/`. `core` and `services` never
import a concrete adapter.

## Rules

1. **Round-trip preserves comments, key order, and formatting.** `parse` → `render` with no
   edit returns the original bytes. This is the first test you write.
2. **Prefer minimal text edits over re-serialization for JSONC.** No library reliably keeps
   both comments and trailing commas while rewriting, so edit the original text at the
   changed key. See the "why" note in `AGENTS.md` §2.5.
3. **Use the round-trip libraries**: `tomlkit` (TOML), `ruamel.yaml` (YAML), `lxml` (XML),
   `plistlib` (plist). Import them **lazily** so CLI startup stays fast.
4. **Key-level, not line-level.** The unit of comparison is a key (or a nested key path),
   not a line. A changed key on one side wins; only a both-sides change conflicts.
5. **Unknown/binary formats have no merge.** `binary` maps to "choose a side" — do not
   invent a structural merge.
6. **No I/O in the adapter.** `parse`/`render` take and return `str`/`bytes`; the caller
   reads and writes. This keeps adapters unit-testable with plain strings.

## Tests

- **Round-trip**: `render(parse(text)) == text` for a fixture with comments and unusual
  order. Put the fixture in `tests/fixtures/` — real key names, not invented.
- **Merge semantics**, parametrized: one-sided change wins; identical change is a no-op
  (idempotent); both-sides different change is the only conflict.
- **Property tests** with hypothesis for the merge-relevant behavior (idempotence; a
  one-sided change always wins).
- **Encoding**: text is read and written as UTF-8.

No `if`/`for`/`while` in test functions (`tests/test_style.py` enforces it). Use
`@pytest.mark.parametrize` and comprehensions.

## Finish

```bash
uv run ruff format && uv run ruff check --fix && uv run ty check && uv run pytest
```

Coverage must clear 90%. Document the adapter's `why` in its module docstring.
