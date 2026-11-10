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
| `datagrip` | `Library/Application Support/JetBrains/@DataGrip/` (logical; the real folder is `DataGrip<year>.<n>`): `options/{editor-font,console-font,terminal-font,colors.scheme,customization,ide.general,diff,editor,code.style.schemes,laf,databaseSettings,csvSettings}.xml` · `options/mac/keymap.xml` · `keymaps/*.xml` · `codestyles/*.xml` · `templates/*.xml` · `colors/*.icls` | `xml` | the `metaInfo` element of a colour scheme (`created`/`modified`/`ide`/`ideVersion`) · `ide.general.xml` Registry `trace.state.event.service.url` | none in a tracked file; `dataSources.xml`, `dataSources.local.xml`, `*.key`, `c.kdbx`, `c.pwd`, `security.xml`, `ssl/` are never tracked and are denied in code | `com.jetbrains.datagrip` | <https://www.jetbrains.com/datagrip/download/> | `datagrip` |
| `clion` | `Library/Application Support/JetBrains/@CLion/` (logical; the real folder is `CLion<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.CLion` | <https://www.jetbrains.com/clion/download/> | `clion` |
| `goland` | `Library/Application Support/JetBrains/@GoLand/` (logical; the real folder is `GoLand<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.goland` | <https://www.jetbrains.com/go/download/> | `goland` |
| `intellij-idea` | `Library/Application Support/JetBrains/@IntelliJIdea/` (logical; the real folder is `IntelliJIdea<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.intellij` | <https://www.jetbrains.com/idea/download/> | `intellij-idea` |
| `phpstorm` | `Library/Application Support/JetBrains/@PhpStorm/` (logical; the real folder is `PhpStorm<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.PhpStorm` | <https://www.jetbrains.com/phpstorm/download/> | `phpstorm` |
| `pycharm` | `Library/Application Support/JetBrains/@PyCharm/` (logical; the real folder is `PyCharm<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.pycharm` | <https://www.jetbrains.com/pycharm/download/> | `pycharm` |
| `rubymine` | `Library/Application Support/JetBrains/@RubyMine/` (logical; the real folder is `RubyMine<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.rubymine` | <https://www.jetbrains.com/ruby/download/> | `rubymine` |
| `rustrover` | `Library/Application Support/JetBrains/@RustRover/` (logical; the real folder is `RustRover<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.rustrover` | <https://www.jetbrains.com/rust/download/> | `rustrover` |
| `webstorm` | `Library/Application Support/JetBrains/@WebStorm/` (logical; the real folder is `WebStorm<year>.<n>`): the same files as `datagrip` without `databaseSettings` and `csvSettings` | `xml` | as `datagrip` | none in a tracked file; the credential-shaped `options/` files are never tracked and are denied in code | `com.jetbrains.WebStorm` | <https://www.jetbrains.com/webstorm/download/> | `webstorm` |
| `orca` | `Library/Application Support/orca/profiles/local-default/@profile-state/settings.json` (virtual: the `settings` row of `profile-state.db`, shown as an indented JSON file; `local-default` only) | `jsonc` (the row's JSON) | `notifications.customSoundPath`; every key outside the allowlist stays local | an exact allowlist of top-level preference keys (`presets/orca.py` `SYNCED`); each credential-shaped key is also named as excluded: `opencodeSessionCookie` · `opencodeGoApiKey` · `codexManagedAccounts` · `claudeManagedAccounts` · `agentDefaultEnv` · `agentCmdOverrides` · `httpProxyUrl` · `mobilePairingCustomAddress(es)` · … | `com.stablyai.orca` | <https://github.com/stablyai/orca/releases> | — (Homebrew's `orca` is another app) |
| `claude-memory` | `.claude/projects/@<name>/memory/*.md` (logical; the real folder is the project's real path with every non-alphanumeric turned into `-`). Per project this machine maps in `agent_projects`; never by default | `text` | — (a memory file has no key paths) | — (the secret scan is the backstop) | — (a CLI) | — | — |
| `claude-sessions` | `.claude/projects/@<name>/{<uuid>.jsonl, <uuid>/subagents/agent-*.{jsonl,meta.json,forked-skill.json,forked-skill.marker.json}, <uuid>/tool-results/*}` (logical; enumerated per session by `ClaudeSessionStore`, never by a glob). Per project this machine maps in `agent_projects`; never by default | `text`, append-only | — | — (a secret-scan hit refuses the **whole session**) | — (a CLI) | — | — |

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

### `jetbrains` (common) and `datagrip` notes

The shared rules live in `presets/jetbrains.py` (a builder, one product is a few lines) and the evidence in
`presets/datagrip.py`. Issue #54.

- **Evidence.** DataGrip 2026.2.6 (`DB-262.10968.148`) inspected 2026-11-09: bundle ID `com.jetbrains.datagrip`,
  `product-info.json` `dataDirectoryName` `DataGrip2026.2`, `productVendor` `JetBrains`; the settings folder is
  `~/Library/Application Support/JetBrains/DataGrip2026.2/`. `options/*.xml` have no XML declaration, a root
  `<application>`, 2-space indent, no trailing newline and ` />` for an empty element (which `lxml` would rewrite,
  hence the minimal-edit XML adapter). Cask `datagrip` verified.
- **D3 What is tracked.** Exact `options/` files, never an `options/*.xml` glob: `editor-font`, `console-font`,
  `terminal-font`, `colors.scheme`, `customization`, `ide.general`, `diff`, plus DataGrip's `databaseSettings` and
  `csvSettings`; and four globs over the user's own schemes (`keymaps/*.xml`, `codestyles/*.xml`, `templates/*.xml`,
  `colors/*.icls`). `editor.xml`, `code.style.schemes.xml`, `laf.xml` and `options/mac/keymap.xml` (`PER_OS`) are
  **source-verified, not seen on the inspected Mac** (an uncustomized folder lacks them): their `@Storage` was read in
  `EditorSettingsExternalizable.java`, `PersistableCodeStyleSchemes.java`, `LafManagerImpl.kt` and
  `KeymapManagerImpl.kt` (intellij-community master, 2026-11-09). `find.xml` is left out: its `<mask>` elements are
  the Find dialog's recent-mask history. Out of scope: `fileTemplates/`, `inspection/`.
- **D4 Never tracked.** All `RoamingType.DISABLED` files (`window.state`, `recentProjects`, `usage.statistics`,
  `features.usage.statistics`, `trusted-paths`, `path.macros`, `updates`, `ide.general.local`, `security`,
  `proxy.settings`, `certificates`, `gitlab`, `remote-servers`, `settingsSyncLocal`), `other.xml`, `window.layouts.xml`,
  `jdk.table.xml`, `databaseDrivers.xml`, `settingsSync*.xml`, `github.xml`, `sshConfigs.xml`, `webServers.xml`,
  `dataSources*.xml`, `*.key`, `c.kdbx`, `c.pwd`, and the folders `consoles/`, `workspace/`, `ssl/`, `jdbc-drivers/`.
  They are in no preset. `system/deny_list.py` blocks the sensitive ones by name in every release folder
  (`options/dataSources*.xml`, so `dataSources.local.xml` and any other spelling too, `options/dataSources/`,
  `options/security.xml`, the credential-shaped files listed under "Denied in code" below, `c.kdbx`, `c.pwd`, `*.key`
  and the folders `ssl`, `jdbc-drivers`, `consoles`, `dataSourcesHistory`, `workspace`, `tasks`, `settingsSync`).
  The rest of that list is local state that is merely in no preset (`window.state.xml`, `other.xml`, ...), and
  `settingsSync*.xml` is read in memory and never tracked, but is not on the deny list.
- **D5 Noise.** In a `.icls`, the whole `metaInfo` element (its `created`/`modified`/`ide`/`ideVersion` properties are
  the `META_INFO_*` names in `AbstractColorsScheme.java`: when and by which product the scheme was saved). The element
  is named, not each property, so a second `<metaInfo>` (a repeated group) and an empty `<metaInfo />` are stripped
  too; a property in it that is not one of the four is not synced either. Changing this appended a `POLICY_HISTORY`
  entry for `datagrip`. In `ide.general.xml` the Registry
  holds `source="SYSTEM"` entries the IDE sets itself, but a key pattern matches a key, never an attribute value, so
  "all SYSTEM entries" cannot be expressed: only `component[Registry]/entry[trace.state.event.service.url]`, the one
  seen, is stripped, and any other IDE-set entry is a known gap that would show as a diff.
- **D6 Settings Sync.** `options/settingsSync.xml` is read **in memory only** (`SettingsSyncSettings`,
  `syncEnabled`, compared case-insensitively, so `true`, `TRUE` and `True` are on). A **missing** file, or a file
  without that key, is off (the IDE default). A file that **exists but cannot be read safely** (a symlink or other
  non-regular file, larger than 64 KiB, not UTF-8, unparseable XML, or one with a DOCTYPE) is *unknown*: reading it as
  off would let two sync tools fight over the same files, so the product is skipped for collect and apply with
  "could not read settingsSync.xml; leaving files alone", and `doctor` fails the row. When on, neither collect nor apply touches that product's
  files, the report says "JetBrains Settings Sync is on - turn off one of the two" (the preset's data), and `doctor`
  fails its config-folders row. The file is never tracked, sealed or written.
- **Defer while running.** There is no VFS tracker for application-level storages, so an outside edit is overwritten
  when the component next saves (focus loss, exit; non-roamable state about every five minutes). `defer_while_running`
  therefore holds an apply that would change a file until the IDE quits; collect still runs.
- **Unverified.** The component name and the nesting inside the `databaseSettings.xml` fixture follow the evidence's
  element names (`csv-format` `name`/`id`, `parameter-pattern` `value`), not a real file; no other field name is
  invented; `editor.xml`, `code.style.schemes.xml`, `laf.xml` and `options/mac/keymap.xml` were not
  observed on a real folder; a macOS run with the IDE open, a real Settings Sync switch, and a second Mac were not
  exercised.

### The other JetBrains IDEs

`presets/jetbrains_ides.py` ships `clion`, `goland`, `intellij-idea`, `phpstorm`, `pycharm`, `rubymine`, `rustrover` and
`webstorm` through the same builder, each as one call with its own metadata. Issue #54, PR D.

A product ships only when its bundle ID, folder prefix, cask and download page are **all** source-verified (2026-11-09):

| Product | Bundle ID (cask `quit:` id and `Preferences/<id>.plist`) | Prefix (cask zap path, vendor help page) | Cask | Download page (HTTP 200) |
| --- | --- | --- | --- | --- |
| CLion | `com.jetbrains.CLion` | `CLion` (`/help/clion/`) | `clion` | `/clion/download/` |
| GoLand | `com.jetbrains.goland` | `GoLand` (`/help/go/`) | `goland` | `/go/download/` |
| IntelliJ IDEA | `com.jetbrains.intellij` | `IntelliJIdea` (`/help/idea/`) | `intellij-idea` | `/idea/download/` |
| PhpStorm | `com.jetbrains.PhpStorm` | `PhpStorm` (`/help/phpstorm/`) | `phpstorm` | `/phpstorm/download/` |
| PyCharm | `com.jetbrains.pycharm` | `PyCharm` (`/help/pycharm/`) | `pycharm` | `/pycharm/download/` |
| RubyMine | `com.jetbrains.rubymine` | `RubyMine` (`/help/ruby/`) | `rubymine` | `/ruby/download/` |
| RustRover | `com.jetbrains.rustrover` | `RustRover` (`/help/rust/`) | `rustrover` | `/rust/download/` |
| WebStorm | `com.jetbrains.WebStorm` | `WebStorm` (`/help/webstorm/`) | `webstorm` | `/webstorm/download/` |

All URLs are under `https://www.jetbrains.com`. Case matters in a bundle ID: `CLion`, `PhpStorm` and `WebStorm` are
capitalised, `rustrover`, `rubymine`, `goland` are not. RubyMine's download page is `/ruby/download/` (`/rubymine/download/`
redirects to a landing page); the RubyMine cask's zap path omits the `JetBrains/` segment and is stale, and the vendor's
help page confirms `~/Library/Application Support/JetBrains/RubyMine<ver>`.

- **No product-specific `options/` file.** The PhpStorm candidates (`php.xml`, `php-tools.xml`, `php-test-framework.xml`,
  `twig.xml`) and RubyMine's (`gemmanager.xml`, `ruby-debugger-type-renderers-settings.xml`) may roam, but those plugins
  are closed source, so no `@Storage` / `RoamingType` could be read; CLion and RustRover have none to confirm. Only the
  common set ships.
- **Denied in code.** The basis of each group is the storage name seen in intellij-community's
  `ide-settings-model.json` (the file names of the connection, deployment, container, Kubernetes and HTTP-client
  settings); that they hold a host, a user name or a token is **inferred, not verified**, and they are blocked
  conservatively. Beside the DataGrip entries, `system/deny_list.py` now blocks, under
  `JetBrains/*/options/`, `remote-servers`, `sshConfigs`, `webServers`, `webDeployTransportHostTargets`,
  `dataSources*`, `databaseDrivers`, `certificates`, `proxy.settings`, `github`, `gitlab`, `docker`, `docker-registry`,
  `kubernetes-settings`, `http-client`, `sshRecentConnections*`, `wslRecentConnections*`, `recentConnections` and
  `trusted-paths` (all `.xml`), folded for case and normalization, symlink-safe; the same names under `options/mac/` too; `JetBrains/Toolbox/apps/...` is not
  matched (a file directly in a sibling's `options/` would be, which is harmless).
- **Not shipped.** Rider (JetBrains' docs place the ReSharper settings at
  `Rider<version>/resharper-host/GlobalSettingsStorage.DotSettings` (https://www.jetbrains.com/help/rider/2026.2/Sharing_Configuration_Options.html), but its `options/` layout and
  whether `GlobalSettingsStorage.DotSettings` is safe to sync (it may hold machine paths, and has its own Settings
  Sync behaviour) are unverified, so it stays out until both are checked), the Community editions
  (`IdeaIC`, `PyCharmCE`; their casks are deprecated), Android Studio (Google's vendor folder, not `JetBrains`).
- **Unverified.** No app of these products was installed on the authoring machine, so a real folder of each was not
  inspected; the format evidence is DataGrip's, and the shared file set is source-verified for every product.

### `orca` notes

Issue #55 (Step 0 and the decision: <https://github.com/devgyurak/syncintosh/issues/55#issuecomment-6734812135>; the
issue body still describes the old `orca-data.json` layout and is stale). Orca 1.4.219 (Stably AI, Electron),
inspected 2026-11-09 with `scripts/inspect_config.py --sqlite-document` (read-only, every string masked); no value was
copied.

- **The source is a database row, not a file.** Since 2026-10-03 the authority is
  `profiles/<id>/profile-state.db` under Orca's data folder (SQLite, WAL, `user_version` 3). `settings` is the row
  `domain = 'settings'` of `profile_state_documents`, a compact JSON payload with a `content_hash`. `orca-data.json`
  beside it is only a compatibility export: Orca records the hash of the JSON it last accepted, and an outside edit
  sends the user to its recovery flow. **It is never read, written or collected** (and is denied in code).
- **A virtual path.** The row reaches `services` as the file `…/profiles/local-default/@profile-state/settings.json`
  (`core/documents.py`, `system/document_files.py`); `@` marks it virtual, so no real file collides with it. It is
  read as indented JSON, so a diff or a resolve does not meet a nine-kilobyte line, and written back compact with the
  key order kept. A write equal to the current value is not made.
- **The write follows Orca's own protocol** (`system/sqlite_documents.py`): one transaction that re-checks the profile
  revision the row was read at, updates the row (`revision + 1`, `updated_at`, `content_hash = sha256(payload)`) and
  the meta `revision`, and nothing else; it never creates the database. A `user_version`, `profile_id`,
  `domain_version` or hash it does not recognise is refused with a fixed message as **one refused file**: the report names the Orca
  document, every other file still syncs, and the record and the apply ledger are left as they were, so nothing is read
  as a deletion. A row that moved between the read and the write is a non-blocking report and the next cycle retries.
- **An allowlist.** 208 top-level keys were seen on the inspected machine. `SYNCED` names the ones that travel
  (appearance, editor, terminal, diff, notification and default-agent preferences); `LOCAL` records the reason for each
  other key seen (paths, migration markers, dismissed prompts, feature flags, command lines, permissions, machine
  identity, and `terminalCustomThemes`, which was empty on the inspected machine so the shape of an entry was never seen);
  a test pins that every observed key is one of synced, local or excluded. A key Orca adds later is **local
  until the preset is updated**: that is why this is not a deny-list. The credential-shaped keys are named as
  `EXCLUDED` as well, and moving one into `SYNCED` is refused at construction.
- **Not verified.** Which settings Orca seals at rest with the OS secret store (the bundle's secret-store modules cover
  credential *files*; no list of sealed keys inside the `settings` row was found). The allowlist holds only plain
  preference values for that reason. The issue's noise keys (`windowBounds`, `uiZoomLevel`, sidebar widths) are **not
  in the `settings` row** on this version: they live in the `ui` domain, which is never read.
- **Keybindings** are not in `settings`; the bundle names a user file `~/.orca/keybindings.json` (absent on the
  inspected machine). Not tracked: a follow-up once a real file can be inspected.
- **Apply waits for Orca and `orcad`.** A running Orca keeps the revision it last saw, so a write from outside stops
  its saving ("Saving has stopped. Restart Orca"). `defer_while_running` holds the apply while the app runs, and
  `system/orca_daemon.py` counts `orcad` (`Orca Helper … daemon-entry.js`, pid in `daemon/daemon-v<N>.pid`, started
  from inside `Orca.app`) as the app: a live pid whose executable is inside `Orca.app` means running, an unreadable pid
  file or path means "cannot tell", which also defers. Collect still runs; it only reads. Not installed also defers,
  with the download page. Whether `orcad` opens the database after the app quits was **not** checked (Orca was
  running), so the check stays on.
- **No watch.** Orca writes `ui` and `workspaceSession` every few seconds, so watching the database would run a cycle
  without end. The virtual path is left out of the watch (`services/profile_paths.py`), and the row is collected by the
  periodic sync (`syncintosh daemon install` runs `watch --poll`) or a manual `sync`.
- **Denied in code** (`system/deny_list.py`): Orca's **whole data folder**, `Library/Application Support/orca/`, as a
  tree. Besides the settings row it holds the secret-protection file, an E2EE key pair, a session-authority key, the
  terminal host's token (`daemon/`), browser session stores (`Partitions/…`, `Network/`), `codex-runtime-home/` auth
  files, `terminal-history/`, `orchestration.db`, the profile database (with `-wal`/`-shm`) and `orca-data.json` (with
  backups and the `.sqlite-export.<revision>.json` copies, which hold the whole state, credentials included). Naming
  them one by one would fail open for the next folder Orca adds. The one exception is the virtual
  `profiles/local-default/@profile-state/settings.json`, exempt only when both its real and its written spelling are
  that view, so a symlink dressed as the view still resolves into the denied tree.
- **One profile.** `local-default` is a constant in Orca's code (the default "Personal" profile); any other profile has
  another id and is not supported (a follow-up).
- **No cask.** Homebrew's `orca` is plotly's unrelated app. The download page is the GitHub release feed the bundle's
  own `app-update.yml` names (`stablyai/orca`); the vendor's site (`orca.dev` in the repository's metadata,
  `www.onorca.dev` in the bundle's help links) was not confirmed as the download page.

## Per-project agent data

A coding agent keeps a project's data under a folder named after the project's **real absolute path**, and two Macs rarely
keep a project at the same path (issue #56). A record therefore carries a **logical** path naming the project, the way a
VS Code profile and a JetBrains release are named: `.claude/projects/@<name>/memory/MEMORY.md`. The leading `@` cannot
start a real folder name (Claude Code spells every non-alphanumeric as `-`), so a logical path never collides with a real
one, and the manifest schema is unchanged; a build that predates this refuses the record at the catalog check.

`<name>` is a label chosen in **this machine's** config, never a path. Mapping is opt-in and per project: nothing is
collected until a project is listed, and a record is written only under a name the config maps.

```json
{
  "agent_projects": [
    {"name": "syncintosh", "path": "Develop/syncintosh", "presets": ["claude-memory", "claude-sessions"]}
  ]
}
```

* `path` is relative to `$HOME` and is the only place the real project is named; another Mac maps the same `name` to its
  own root (`code/syncintosh`). The folder is derived from the root's real path (symlinks resolved), by the CLI's own rule:
  NFC, every character outside `[a-zA-Z0-9]` becomes `-` (a character outside the BMP is two), a name over 200 characters is
  cut to 200 plus `-` and a base-36 hash of the path.
* `presets` lists what travels for the project; `init --preset claude-memory` is refused, because it would track nothing.
* The root must exist on this Mac (apply defers until the project is cloned) and must not be a symlink, a dangerous path,
  or leave `$HOME`. The agent's own folder is created on apply if the agent has not run in the project yet.
* **Refused, and shown in `syncintosh doctor`:** `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_PROJECT_DIR_NAME`, `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` or
  `CLAUDE_CODE_REMOTE_MEMORY_DIR` set. The check reads the environment of the process that runs the sync: the launchd daemon
  gets only `PATH`, so a variable exported in `.zshrc` is seen by `sync` and `doctor` but **not** by the daemon. Do not
  relocate Claude's data and run the daemon without telling it (not detected, a documented limit). A layout is refused
  because its derived folder would be wrong. **Skipped for the project:** `autoMemoryDirectory` in `~/.claude/settings.json` or the project's
  `.claude/settings{,.local}.json` (only that key is read; a file that cannot be read counts as set).
* v1 maps the root the person names, which for memory is the repository's canonical git root (memory is shared by its
  worktrees). A worktree is not mapped separately.
* The deny-list refuses every other path the agents keep, even when a config names it: `~/.claude.json*`,
  `.claude/{session-env,shell-snapshots,file-history,paste-cache,cache,sessions,daemon*,history.jsonl}`,
  `.codex/{auth.json,installation_id,history.jsonl,shell_snapshots,sqlite,*.sqlite*,thread-writer-locks,app-server-*,ipc}`.

A secret-scan hit in a memory file stops the cycle like any other file (there is no per-session refusal for memory); the
`claude-sessions` preset below refuses a whole session instead.

### `claude-sessions` notes

Session history (issue #56) rides on `docs/design/07-session-history.md`: append-only text, one unit per session, idle and
caps, a whole-session refusal on a secret-scan hit, prefix-only apply with a fresh mtime. Claude Code specifics:

* **Workflows**: 2.1.308 also keeps a workflow's sub-agents under
  `<uuid>/subagents/workflows/wf_<id>/agent-*.{jsonl,meta.json,forked-skill.json,forked-skill.marker.json}`. They are listed
  with the session (one run level, regular files only; a link or an odd entry leaves the session out; a folder whose name is
  not a run id, `/^wf_[a-z0-9-]{6,}$/` as the binary spells it, is not part of the session). UNVERIFIED: that those are the
  only file kinds there; they are assumed to match `subagents/`.
* **Layout** (2.1.308): `<uuid>.jsonl` plus `<uuid>/subagents/agent-*.{jsonl,meta.json,forked-skill.json,
  forked-skill.marker.json}` and `<uuid>/tool-results/*`. `{uuid}` in a preset path is exactly one UUID, so a record cannot
  name another folder under a project; every other name beside a transcript is left alone.
* **Resume without rewriting `cwd`.** `--resume <id>` tries the cwd folder, worktree folders, then every project folder; the
  picker hides a session only when its recorded `cwd` exists here, is another directory and encodes to the same folder name.
  `cwd` is never rewritten. Open caveat: anthropics/claude-code#58475 reports an empty project-scoped picker after a move that
  the code does not explain; check `claude --resume` on a second Mac before a release.
* **Open session.** `~/.claude/sessions/<pid>.json` (`pid`, `sessionId`, `procStart`) is written on start and removed on exit,
  and survives a crash. A session is open only when the pid is alive **and** `procStart` equals `ps -o lstart= -p <pid>`
  (`LC_ALL=C TZ=UTC`), the CLI's own check. A file that cannot be read leaves the answer "cannot tell", which counts as open;
  a Mac with no such folder has no open session.
* **mtime.** `cleanupPeriodDays` (default 30) deletes a `.jsonl` older than that by **mtime**, with its `<uuid>/` folder, so
  apply writes with a fresh mtime.
* **Version.** After a transcript is written, its first record's `version` is compared with `claude --version`; a different
  one prints a one-line note, and an unknown one prints nothing. It never refuses.
* **Memory is separate.** `claude-memory` and `claude-sessions` are enabled per project independently; neither implies the other.
* **Never collected** (deny-listed): `~/.claude.json*`, `session-env/`, `shell-snapshots/`, `file-history/`, `paste-cache/`,
  `history.jsonl`, `cache/`, `daemon*`, and `sessions/` itself. Without `file-history/`, `/rewind` cannot restore code on the
  other Mac.

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