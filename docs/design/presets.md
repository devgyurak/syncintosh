# Preset reference table

**Status:** Living table. One row per built-in preset, added **only** after Step 0 of the
[`add-preset` skill](../../.claude/skills/add-preset/SKILL.md) has verified the values — on a Mac
that actually has the app, or, where the app is not installed and the user has approved the
exception, against the vendor's source and documentation with the sources named in the row's
notes. Filled in by [#6](https://github.com/devgyurak/syncintosh/issues/6).

A preset's `noise` and `excluded` keys are cut on the **collect** side, before the object is
sealed, and re-attached from the live file on apply — so a noise change is invisible to the diff
and an excluded value is in no object at all ([02-base-and-merge.md](02-base-and-merge.md),
"Projection").

| Preset | Paths (relative to `$HOME`) | Format | Noise keys | Excluded in code | Bundle ID | Download URL | Cask |
|---|---|---|---|---|---|---|---|
| `zsh` | `.zshenv` · `.zprofile` · `.zshrc` · `.zlogin` · `.zlogout` | `text` | — (a shell file has no key paths) | — (the secret scan is the backstop) | — | — | — (ships with macOS) |
| `vscode` | `Library/Application Support/Code/User/settings.json` · `…/keybindings.json` · `…/snippets/*.json` · `…/snippets/*.code-snippets` | `jsonc` | the settings `userDataSync` computes as not synced — `window.zoomLevel`, `keyboard.layout`, `files.dialog.defaultPath`, `git.path`, `typescript.npm`, `php.validate.executablePath`, … | `http.proxyAuthorization` (dotted and nested) | `com.microsoft.VSCode` | <https://code.visualstudio.com/download> | `visual-studio-code` |
| `docker` | `.docker/config.json` · `.docker/daemon.json` | `jsonc` (plain JSON is a subset) | `currentContext` · `credsStore` · `credHelpers` (config) | `auths` · `proxies` · `HttpHeaders` (config) · `proxies` (daemon) | `com.docker.docker` | <https://www.docker.com/products/docker-desktop/> | `docker-desktop` |

### `docker` notes

- Docker Desktop 4.91.0, inspected 2026-10 with
  `scripts/inspect_config.py --shape-strings` (the sanctioned way to read a secret-shaped file,
  `AGENTS.md` §6). Bundle ID from `Docker.app/Contents/Info.plist`; the cask from
  `brew info --cask docker-desktop`.
- `auths` was observed in the real file — it is the store `docker login` falls back to. The other
  three exclusions were **absent** on the inspected machine but are documented carriers, so they
  are cut by construction rather than left to the user (`AGENTS.md` §3.4):
  - `proxies` — the CLI reference shows `httpProxy: "http://user:pass@host:port"` and warns that
    proxy settings "may contain sensitive information"; the daemon reference documents the same
    shape under a top-level `proxies` in `daemon.json`.
  - `HttpHeaders` — a map of *arbitrary* headers sent on every client→daemon message, which is a
    documented place for `Authorization: Bearer …`. The secret scan matches known token shapes, not
    "this value looks like a header", so nothing else would catch it. A custom header set must
    therefore be reproduced per machine.
- `currentContext` is the endpoint this machine talks to, and `credsStore`/`credHelpers` name a
  credential-helper binary that exists **on this machine** (`docker-credential-<value>`). Syncing
  either would flip the other Mac's context, or point it at a helper it does not have — a Mac
  running Docker Desktop stores `desktop` where colima or OrbStack stores `osxkeychain`, and the
  next `docker pull` there would fail with `executable file not found` while its own stored logins
  became unreachable. All three are `noise`, so each machine keeps its own.
- The `plugins` map, by contrast, is synced: Step 0 showed its `hooks` values are neither paths nor
  booleans, so there is no evidence they are machine-local — a follow-up noise entry if they churn.
- `~/.docker/contexts/tls/` is in the deny-list (`system/deny_list.py`). `certs.d/`,
  `~/.docker/tls/`, and the `ca.pem`/`cert.pem`/`key.pem` defaults the CLI reference documents
  were absent on the inspected machine, so they are not denied yet: a user would have to add such
  a path explicitly, and the private-key secret scan is the backstop that keeps it out of the repo.
  Tracked as a follow-up rather than guessed at now.

### `zsh` notes

- The five files zsh reads at startup, verified on a Mac (zsh 5.9, 2026-10). `ZDOTDIR` is **not**
  honoured: a user who moves the startup files names paths this preset does not know, and the
  catalog check refuses them at collect (nothing is published for them) and again at apply (a
  record that arrived from elsewhere cannot reach them) rather than guessing.
- `noise`/`excluded` are empty because `text` has no key paths to strip. A line-level merge of two
  changed regions can still produce a file that does not parse, so the merge result and every
  write are checked with `zsh -fn` (parse only, `-f` so no rc file runs).
- A `~/.zshrc` that is a symlink is reported, never written through
  ([03-manifest.md](03-manifest.md), "`~/.zshrc` that is a symlink").

### `vscode` notes

- **Verified against the source, not an installed app** (the approved exception for this preset,
  issue #6 decision V4). `microsoft/vscode` `main`,
  `src/vs/platform/userDataSync/common/userDataSync.ts` (`getIgnoredSettings`) is what defines
  "not synced": a setting whose registration has `ignoreSync: true`, or `scope: MACHINE`, or
  `scope: MACHINE_OVERRIDABLE`. There is no hard-coded list any more, so this preset's noise list
  is those setting ids. Also consulted: `configurationEditing.ts` (`setProperty` edits only the
  value's span, so comments survive) and `diskFileSystemProvider.ts` (an atomic `.vsctmp` + rename
  save, so a copy-based collect never reads a half-written file), plus
  `code.visualstudio.com/docs/configure/settings` and `brew info --cask visual-studio-code`.
- `terminal.integrated.*` is **deliberately synced** — a setting VS Code syncs is a setting we
  sync. A `noise` entry is a *match* against the file: a stale entry is a no-op while a missing one
  is churn on every sync, so the list errs towards completeness.
- `http.proxyAuthorization` is registered in **both** shapes a file can spell it (the dotted key
  VS Code writes, and a nested object), so neither can carry a credential into the repo. The secret
  scan gained a `scheme://user:pass@` pattern as the backstop for a proxy URL written under *any
  other* key (`system/secret_scan.py`) — and it matches a *literal* password only: `$DB_PASS` and
  `${PGPASSWORD}` are references, which is the recommended way to keep the value out of the file,
  so a URL that uses them is not refused.
- `keybindings.json` has **no** strip list: the merge compares an array whole, because a later
  binding overrides an earlier one, so identifying entries by key would change behaviour. A change
  to the same binding on both machines is therefore a whole-file conflict.
- A key that a machine never set simply does not arrive: a machine that has never had the file gets
  the synced settings without the machine-local ones, which then take VS Code's own defaults. That
  is what "not synced" means for a machine that never chose a value.
- User snippets are two **globs** (`snippets/*.json` and `snippets/*.code-snippets`), verified
  against `code.visualstudio.com/docs/editor/userdefinedsnippets`: a language snippet is
  `snippets/javascript.json`, a global one is `snippets/<name>.code-snippets`, and the files are
  "written in JSON, support C-style comments". Which files exist is per-machine, so the preset names
  the directory and the two suffixes rather than exact paths; a snippet the other machine created is
  written here only because this machine's own config names the same glob
  ([01-sync-model.md](01-sync-model.md), "Preset globs").
- **Out of scope:** a non-default profile (`User/profiles/<id>/`, refused by the apply-side catalog
  check rather than guessed at), `settingsSync.ignoredSettings`, `tasks.json`, `mcp.json` (its server
  `env` and `headers` need their own exclusion pass before it can be collected safely), Insiders,
  portable mode, `--user-data-dir`, and project snippets (`<project>/.vscode/*.code-snippets`, which
  are not under the `$HOME` user directory).

## How a row gets filled

1. Inspect the real config — path, format, and the noise keys that would otherwise conflict on
   every sync (skill Step 0-1 … 0-3).
2. Nail the exclusions that must be stripped **in code**, not left to the user (skill Step
   0-4). Docker's `auths` and context TLS certificates are the canonical example.
3. Verify the bundle ID (for app detection), the vendor's official download URL, and the
   Homebrew cask (skill Step 0-5).
4. Record the evidence block (skill Step 0-6) in the preset module docstring and the PR body,
   then add the row here.
5. If a value cannot be verified, **stop** (skill Step 0-7) — a guessed path collects nothing
   and looks like success.

Only the URLs hard-coded in the built-in presets this table describes are trusted
(`AGENTS.md` §3.6). A URL that arrives from the LLM, or from a synced or community preset, is
**untrusted**: label it and show its domain.