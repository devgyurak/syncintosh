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
| `zed` | `.config/zed/settings.json` · `…/keymap.json` · `…/snippets/*.json` · `…/themes/*.json` | `jsonc` | `node.path` · `lsp.*.binary.path`/`arguments` · `terminal.shell` · `ssh_connections` · `session.trust_all_worktrees` · … (see the notes) | `context_servers.*.env`/`headers`/`oauth`/`settings` · `agent_servers.*.env` · `terminal.env` · `language_models.*.custom_headers` (and `language_models.openai_compatible`/`anthropic_compatible`.`*.custom_headers`) · `dev_container_connections` · … (each under the root, the four channels, the three platforms and `profiles.*.settings`) | `dev.zed.Zed` | <https://zed.dev/download> | `zed` |

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
- `keybindings.json` has **no** strip list, but its array merges **one binding at a time** (issue #21).
  The preset declares an `ArrayRule` for the root array: identity `key`, `command`, `when`; required
  `key` and `command`; group `key` and `when`. A binding is in the group of its `key` and `when`, and
  a group one machine changed is taken from that machine. A group both machines changed differently is
  a conflict on that binding alone, and the resolve chooser and the model are asked about that binding.
  Order is kept: the local order stays, and an addition goes after its predecessor on its own side. Two
  machines that add different bindings both keep them. `key` is compared exactly, so `Ctrl+A` and
  `ctrl+a` are two groups, although VS Code reads them as one; the file text is never normalized.
  A removed binding takes its notes with it (see [02-base-and-merge.md](02-base-and-merge.md)). Two
  machines that reorder the shared bindings differently are one whole-array conflict, which says that a
  per-binding pick cannot settle it and that a whole side drops the other side's additions. An array this
  rule cannot read (a non-object element, a missing `key` or `command`, two bindings with one identity)
  falls back to the whole-array conflict the file had before.
  Layout limits of the rewrite (blank lines beside the brackets, a comma-first array) are listed in
  [02-base-and-merge.md](02-base-and-merge.md).
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
- **Profiles** (issue #22) are matched **by name**, because a profile's directory is a random id
  (`hash(uuid).toString(16)`, a signed int32, so it can start with `-`) that two Macs do not share. A
  record's path is **logical**: `…/Code/User/profiles/@<name>/<file>`, the name encoded with
  `quote(name, safe="")`, so `/` `*` `?` `@` `%` can never change the path's shape. Each profile
  contributes one record per file (`settings.json`, `keybindings.json`, and the two snippet globs), and a
  profile's file takes the **default profile's** policy: the same noise keys, the same `http.proxyAuthorization`
  exclusion, the keybindings array rule, and the JSON check. The real directory is looked up on each
  machine from VS Code's own list, `User/globalStorage/storage.json` → `userDataProfiles`
  (`{name, location, icon?, useDefaultFlags?}`; `location` is the id, or an older `file` URI object whose path is exactly this machine's
  profiles home plus the id; a URI elsewhere, or one that names a host, is skipped). That file is read **in memory, for that key only** — it also holds window state
  and a telemetry machine id — and is never collected, sealed or tracked. Evidence: source-verified
  (the approved Step 0 exception, as for the rest of this preset): `microsoft/vscode` `main` `a48dd23e2ac5`,
  `userDataProfile.ts`, `userDataProfilesManifestSync.ts` and `stateService.ts`.
  - A profile is synced only when **this machine's config allows its name** (`profiles` entry,
    `names: "*"` or a list; `init --preset vscode` writes `"*"`) **and** the name resolves in the local list.
    A profile id from a record is never an allowlist: a raw-id path (`…/profiles/-3f2a1b/settings.json`) is
    owned by no preset and is refused.
  - A profile this Mac lacks is a **non-blocking deferral** ("create the profile "Work" in the app"). syncintosh
    never creates a profile directory and never edits `storage.json`; once the person creates the profile,
    the next sync applies it. A profile that is listed but has no folder yet waits too.
  - A resource the profile **borrows** from the default one (`useDefaultFlags`) is not collected and not
    written: VS Code does not read the profile's own copy of it.
  - `builtin/*` (the Agents profile) and malformed entries are skipped and reported; two profiles whose names
    fold to one (`collision_key`), or that share a directory, are both skipped. A missing, unreadable or corrupt
    list resolves **no** profile, is reported, and keeps every record: a profile that cannot be seen is not a
    deleted one. A profile directory that is a symbolic link, a denied path, or leaves `$HOME` is refused.
  - **Known limit:** renaming a profile leaves the old name's record behind (a record is removed only by
    untracking it). Tracked in #67.
  - To add profiles to an existing config by hand, add
    `"profiles": [{"preset": "vscode", "scope": "common", "names": "*"}]` to `config.json`. A `preset add`
    command is tracked in #68.
- **The user's ignore list** (`settingsSync.ignoredSettings`, issue #23). VS Code does not delete an ignored key
  from what it uploads; it leaves the remote's value in place, and the setting is `disallowSyncIgnore`, so the
  list itself **syncs**. syncintosh does the same: an ignored key is **carried**, not stripped
  ([02-base-and-merge.md](02-base-and-merge.md), "Carrying an ignored key").
  - Read once per cycle, in memory, from the default profile's `settings.json`; the default file and every
    profile's `settings.json` obey it. A change to the list is seen from the next cycle.
  - Both `settingsSync.ignoredSettings` and the legacy `sync.ignoredSettings` are read. Matching is by top-level
    key (`"[python]"` is one key). `settingsSync.ignoredSettings` and `settingsSync.ignoredExtensions` cannot be
    ignored.
  - Precedence: **excluded > preset noise > the user's list.** The keys actually carried are the user's list
    minus what the preset strips, so listing `http.proxyAuthorization` seals nothing more and nothing less.
  - `-x` cancels only an `x` the user listed. It does not switch a noise key back on or release an excluded one
    (VS Code uses `-x` to subtract from its own built-in default set, which this tool does not have); such an
    entry is counted in a note on the report. Re-enabling a noise key this way is a follow-up.
  - A default `settings.json` that is a symbolic link, a denied path, or outside `$HOME` has a list that cannot be
    trusted, so a profile's `settings.json` is **not synced** either (the list is unknown, and an empty one would seal
    the keys the user meant to keep local). A default file that is simply absent has no list, as in VS Code.
  - A list that is not an array of strings (or a `settings.json` that cannot be read to find it) **fails closed**:
    the cycle refuses to collect or apply every file that obeys it and reports a fixed message that does not quote
    the file.
- **Out of scope:** `tasks.json`, `mcp.json` (its server `env` and `headers`
  need their own exclusion pass before it can be collected safely), `extensions.json`, `prompts/`, Insiders,
  portable mode, `--user-data-dir`, and project snippets (`<project>/.vscode/*.code-snippets`, which are not
  under the `$HOME` user directory).

### `zed` notes

- **Verified against the source, not an installed app** (a source-verified exception, approved as for `vscode`).
  `zed-industries/zed` `main` @ `dcf3f98c9a3213b533f6d9e5b1790482659964f4` and the Homebrew cask `zed`. Files:
  `crates/paths/src/paths.rs` (`config_dir()`), `crates/settings_content/src/{settings_content,project,agent,
  terminal,language_model,workspace}.rs`, `assets/settings/initial_user_settings.json`,
  `docs/src/reference/all-settings.md`, `docs/src/macos.md`, `crates/zed/Cargo.toml` (bundle id),
  `crates/zed_credentials_provider/src/zed_credentials_provider.rs` (`development_credentials`).
- **Paths.** On macOS the config directory is `~/.config/zed` and `XDG_CONFIG_HOME` does not move it;
  `--user-data-dir` is out of scope. `docs/src/remote-development.md` says `~/.zed`; the code and the other docs
  disagree with it, and the code wins.
- **Overrides.** `UserSettingsContent` flattens `ReleaseChannelOverrides` (`dev`, `nightly`, `preview`, `stable`),
  `PlatformOverrides` (`macos`, `linux`, `windows`) and `profiles: {name: {base, settings}}` over the base, and an
  override does not nest another. Every rule is therefore generated under nine prefixes (the root and these eight);
  the profile name is the user's, which is why `ANY_KEY` exists (`02-base-and-merge.md`, "Projection").
- **Excluded**: any object a credential can sit in. `context_servers.<name>` `env`, `headers`, `oauth` (a
  `client_secret`), `settings` (an extension MCP server's free-form JSON, where a token goes) and the legacy
  `command.env`; `agent_servers.*.env`, `lsp.*.binary.env`, `dap.*.env`, `terminal.env`;
  `language_models.*.custom_headers` (four levels for `openai_compatible` and `anthropic_compatible`:
  `language_models.<kind>.<name>.custom_headers`, because those two are maps keyed by a name the user chose, so
  both get their own rule) and the unread plaintext slot `language_models.lmstudio.api_key`;
  `dev_container_connections` whole (an array whose elements carry `remote_env`).
- **Two consequences of excluding whole objects.** An extension MCP server that has only `settings` is stripped
  whole, so *the server's existence* is not carried to another Mac. And when another Mac deletes a server or a
  profile, this Mac's apply removes that item's local values (its `env`, say) with it: a user-named item the merge
  deleted is not left behind as a server with no `command` (`02-base-and-merge.md`, "Projection"). Stripped values
  are never in a commit, so a restore point does not hold them and **the loss cannot be recovered**; the sync result
  lists it under "removed with an item the synced settings do not have (not recoverable)" (file, item, number of local
  keys, never a value). The same notice appears when an `--adopt remote` overwrite that is written removes a server
  the repo never had.
- **Synced, left to the secret scan**: `lsp.*.initialization_options`, `lsp.*.settings`, `proxy` (the scan knows
  `scheme://user:pass@`), `context_servers.*.command` and `.args`. Also synced, and not yet reviewed against the pinned schema (a follow-up):
  `context_servers.*.url`, `agent_servers.*.command`/`args`, and the `api_url` of `language_models` and
  `edit_predictions` providers, which can embed `user:pass@` or a query-string token; the scan is their only backstop.
- **Noise**, each with its reason in `presets/zed.py`: `node.path`/`npm_path`, `lsp.*.binary.path`/`arguments`,
  `dap.*.binary`/`args`, `journal.path`, `git.worktree_directory`, `terminal.shell` (Intel `/usr/local` vs. Apple
  silicon `/opt/homebrew`), `terminal.working_directory.always`, `agent.sandbox_permissions` ("Allow always"
  records an absolute path), `session.trust_all_worktrees`, `cli_default_open_behavior`, `feature_flags`,
  `ssh_connections` (opening a remote project makes Zed add `projects` itself, so syncing it would conflict every
  time) and `wsl_connections`. Font size is **not** noise: `Increase/Decrease*FontSize` has `persist` false by
  default (`zed.rs`, the default keymap).
- **Keymap.** `keymap.json` is compared as one array. `context` is optional and defaults to `""`, which does not
  fit the rule that an absent field is not `""`; duplicate contexts are allowed and the later entry wins. A
  per-block merge is a follow-up. `snippets/*.json` and `themes/*.json` are globs with no strip list.
- **No ignore list**: Zed has no Settings Sync.
- **Deny list.** `.config/zed/development_credentials` is the Dev build's plaintext sign-in file; it is denied by
  construction (`system/deny_list.py`).
- **Writes.** Settings are edited by tree-sitter minimal edits plus an atomic write; the keymap editor edits minimally
  but writes with a plain `fs.write`, so a copy-based collect can in principle read a keymap mid-write.
- **Migrator (UNVERIFIED).** `zed/src/zed/migrate.rs` converts in memory and rewrites the file only on the banner's
  "Backup and Update"; a write at startup was not seen. Known risk, recorded and not fixed: with different Zed
  versions on two Macs, a key format migrated on one reaches the older one.
- **Out of scope**: `tasks.json` and `debug.json` (the root is an array and `env` sits in its elements, so no key
  path can cut it), `prompts/` (LMDB), `embeddings/`, `prompt_overrides/`, `*_backup.json`, `global_settings.json`,
  `git/config`, `AGENTS.md` (a follow-up candidate), a project's `.zed/`, extensions, and a bundle check for Preview
  or Nightly alone.

## Versioned config directories

A preset may set `config_dir` (`core/config_dirs.py`) when its app keeps settings under a folder named after the
release. Its file paths are then **logical** — `Library/Application Support/JetBrains/@DataGrip/options/…` — and the
real folder is whatever the installed app names in `Contents/Resources/product-info.json` (`dataDirectoryName`,
checked against the preset's prefix and vendor). Rules for such a preset:

- It cannot also have `profiles`, and its bundle ID must be the layout's.
- A folder that does not resolve, or does not exist, defers; it is **never created** (it protects the IDE's
  first-run settings import).
- `defer_while_running` holds back only the **apply** while the app is open (`lsappinfo`); collect proceeds. When
  the check cannot answer, the apply waits too. The flag is data; `services` names no app.
- Credential files of the release folder are denied in code whatever the catalog lists (`system/deny_list.py`).
- Not supported: `idea.config.path` overrides (like `ZDOTDIR`), EAP builds. Several installs of one bundle ID use the
  highest release, and `doctor` says so.
- `config_dir` and `defer_while_running` are local behaviour, not object bytes, so they are not in the policy digest;
  the logical paths are (they are the files' paths).

## Changing a policy

A preset's **policy** is what decides which bytes an object holds — its files, formats, `noise` and `excluded`
keys, `ignore_list` flag, and profile layout. Each one is part of a digest the manifest stamps
([03-manifest.md](03-manifest.md), "Policy stamp"), so changing any of them changes the digest. In the same
commit, **append** the new digest to `POLICY_HISTORY[<preset>]` in `src/syncintosh/presets/history.py` **and** to
`tests/fixtures/policy_history_shipped.json`. `tests/unit/test_policy_history.py` fails until you do, and prints
the digest to append. Never edit or reorder an earlier entry: every repo stamped under it would read as
`unknown`, and the fixture fails if you do.

The digest sees only data. If you change what the **code** does with a preset's files — the semantics of
`core/projection.py` or `core/ignore.py`, or a strip you switch back on — no field changes, so raise that
preset's `policy_epoch` first, then append the resulting digest. Array rules, validators, and download
metadata are not part of the policy and need no entry.

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