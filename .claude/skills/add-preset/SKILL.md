---
name: add-preset
description: Add a built-in syncintosh preset (zsh, vscode, jetbrains, zed, orca, docker, brew, …). Use on "새 프리셋 추가" (add a preset), "VS Code 프리셋 붙여줘", or "JetBrains 설정 동기화 넣어줘". Verifies the real config structure, noise keys, bundle ID, and download URL first, then wires catalog → adapter → ignore list → tests.
---

# Add a built-in preset

> [!IMPORTANT]
> **Do not add a preset before inspecting the real config files.** Step 0 is not optional.

## Why you must look first

A preset guesses three things that fail **silently** if wrong:

- **Paths** — a wrong path collects nothing, and "nothing to sync" looks like success.
- **Noise keys** — keys that change on every run (window position, recent projects,
  update-check timestamps) turn into a conflict on *every* sync. If you miss them, the
  user gets endless conflict prompts and blames the merge.
- **Download metadata** — a wrong bundle ID makes "not installed" detection wrong; a wrong
  URL sends the user to the wrong page.

None of these raise. They just quietly annoy. So the rule is: **only what you observed goes
into code.**

---

## 0. Inspect the real environment (mandatory)

### 0-1. Locate the config files
Confirm the actual paths on a Mac that has the app. Use `docs/design/presets.md` as a lead, not
a fact. `mdfind "kMDItemCFBundleIdentifier == '…'"` finds the app bundle;
`~/Library/Application Support/<app>/` holds settings.

### 0-2. Record the format
- JSONC (VS Code, Zed) — comments and trailing commas. Note that the adapter must do
  minimal text edits, not a full re-serialize.
- XML (JetBrains) — `options/`, `keymaps/`, `codestyles/`, `templates/`.
- plist (Docker Desktop, older versions) — convert to XML to compare.

### 0-3. Identify noise keys
Open the file, note keys that look machine- or session-specific: window geometry, last
opened project, "check for updates" timestamps, telemetry ids, absolute paths, font lists.
Cross-check with a second sample if you have one.

### 0-4. Nail the secret exclusions
Anything credential-shaped is excluded **in code**, not left to the user:
- Docker `config.json` → `auths` and context TLS certificates.
- DB clients → connection strings and credentials.

The guard blocks reading secret-shaped files directly (Docker `config.json`, anything with
`auths`, `*.pem`). To learn their shape without touching the values, use the sanctioned
inspector — it prints the key structure with every value masked:

```bash
uv run python scripts/inspect_config.py ~/.docker/config.json
```

`tests/fixtures/**` is exempt from the guard and from `.gitignore`, so you can commit
Docker TLS fixtures (`ca.pem`, `cert.pem`, `key.pem`) needed by the exclusion tests.

Add `--shape-strings` when the question is *is this value machine-local?*: it prints
`<path>` / `<bool>` / `<str:N>` instead of only the length, so a per-machine plugin path can be
told from a user preference without either value being shown. Never print a value to answer that
question yourself.

### 0-5. Verify download metadata
Record for the preset:
- the **bundle ID** (for `mdfind` detection),
- the **official download URL** (from the vendor, not a mirror),
- the **Homebrew cask name** (verify with `brew info --cask <name>`).

Only these hard-coded values are trusted (AGENTS.md §3.6). A URL that arrives later from an
LLM or a synced/community preset is not.

### 0-6. Record the evidence
Summarize what you found. It goes in the preset module docstring and the PR body:

```
App: Zed 0.x (inspected 2026-10)
Bundle ID: dev.zed.Zed
Paths:    ~/.config/zed/{settings.json,keymap.json,tasks.json,snippets/,themes/}
Format:   JSONC (comments + trailing commas)
Noise:    "vim_mode" ok; "telemetry" timestamp, window geometry
Excluded: none credential-bearing
Download: https://zed.dev/download   cask: zed
```

### 0-7. If you cannot verify, stop
If you cannot confirm the path, format, or a metadata value, **stop and report it**. Code
written from "probably" is code that fails silently and costs far more to debug later.

---

## 1. Preset catalog entry

Add the preset to `src/syncintosh/presets/`: paths, format, noise keys, exclusions, bundle
ID, download URL, cask. Data lives here; behavior does not. `presets/` is infrastructure and
depends only on `core/`.

## 2. Adapter (reuse before you write)

- JSONC → the VS Code/Zed JSONC adapter (minimal text edits).
- XML → the XML adapter.
- plist → the plist adapter (XML conversion).
Only add a new adapter if no existing one fits; if you do, use the `add-adapter` skill.

## 3. Noise keys, exclusions, and machine-only defaults

- Register the noise keys you found as `PresetFile.noise`. They are **cut on the collect side**
  (`core/projection.py`), so a change to one produces no diff, no conflict, and no commit, and
  the machine's own value is put back on apply. Do not rely on the merge's `ignore` set alone —
  that keeps a key out of a conflict but still writes its value into the object.
- Register credential-shaped keys as `PresetFile.excluded`. Same mechanism, different meaning:
  an excluded key must be **in no object at all** (`AGENTS.md` §3.4).
- Both lists are **key paths**, so they only exist for a keyed format (`jsonc`, `toml`, `yaml`,
  `xml`, `plist`). A `text`/`binary` preset names neither; `PresetFile.create` refuses it.
- A value that differs per machine but lives **inside an otherwise-shared file** is `noise` — its
  own value stays on each machine and is never aligned. A whole file that belongs to one machine
  is a different thing: that is a `scope = "machine"` item, which every machine carries but only
  its own writes (`docs/design/02-base-and-merge.md`).

## 4. Tests

Add to `tests/`:
- a **unit** test that normalizes a real-shaped fixture and drops the noise keys;
- a property test that a noise-only change leaves the projection identical, and that
  `project(restore(repo, live)) == repo` (`tests/unit/test_projection.py` is the model);
- an **integration** test that runs collect → merge → apply for the preset end to end, with
  the app "not installed" case asserting **apply is deferred**;
- a test that the secret exclusion actually removes `auths` (Docker) if applicable, and a test
  that this machine's own value survives an apply.

Sample files go in `tests/fixtures/` with **real key names** — do not invent them.

## 5. Finish

```bash
uv run ruff format && uv run ruff check --fix && uv run ty check && uv run pytest
```

Coverage must clear 90%. Update the preset table (`docs/design/presets.md`) with the preset's
row, and put the 0-6 evidence in the PR body.
