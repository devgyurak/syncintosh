# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `zed` preset: settings, keymap, snippets and themes, source-verified.

**Evidence: verified against the source; not inspected on a real machine** (the user approved that
exception for this preset, as for `vscode`, decision V4 of issue #6). Zed is not installed on the machine this
was written on, so every value below comes from the code and the vendor's documentation:

```
App:       Zed (stable; Preview/Nightly share the config dir)
Bundle ID: dev.zed.Zed                 (crates/zed/Cargo.toml bundle metadata; cask `uninstall quit`)
Paths:     ~/.config/zed/settings.json   (JSONC object)                   crates/paths/src/paths.rs config_dir()
           ~/.config/zed/keymap.json     (JSONC array, compared whole)
           ~/.config/zed/snippets/*.json (snippets.json = global, <lang>.json)   snippet_provider/src/lib.rs
           ~/.config/zed/themes/*.json   (theme families)                 theme_settings.rs
Format:    serde_json_lenient — comments + trailing commas
Writes:    settings: tree-sitter minimal edits + atomic_write (settings_store.rs, settings_json.rs)
           keymap editor: minimal edits, plain fs.write (not atomic)
Download:  https://zed.dev/download   (docs/src/macos.md)   cask: zed
```

Source: `zed-industries/zed` `main` @ `dcf3f98c9a3213b533f6d9e5b1790482659964f4`, and the Homebrew cask `zed`.
The key names below are the ones in `crates/settings_content/src/{settings_content,project,agent,terminal,
language_model,workspace}.rs`, `assets/settings/initial_user_settings.json` and `docs/src/reference/all-settings.md`
at that commit. `development_credentials` is `config_dir().join("development_credentials")` in
`crates/zed_credentials_provider/src/zed_credentials_provider.rs` (a plaintext file the Dev build writes).

## One policy, nine places

`UserSettingsContent` (`settings_content.rs`) flattens three override layers over the base settings:
`ReleaseChannelOverrides` (`dev`, `nightly`, `preview`, `stable`), `PlatformOverrides` (`macos`, `linux`,
`windows`) and `profiles: {name: {base, settings}}`. An override holds the same settings shape as the root, and an
override does not nest another override. A credential written under `preview` is as much a credential as one at the
root, so every rule below is generated for each of the nine prefixes: the root, the four channels, the three
platforms, and `profiles.<name>.settings` (the profile name is the user's, hence `ANY_KEY`).

## Excluded: any object a credential can sit in

- `context_servers.<name>`: `env`, `headers`, `oauth` (a pre-registered `client_secret`), and `settings` — an
  extension MCP server's arbitrary JSON, where a token such as a GitHub PAT goes. `command.env` is the legacy shape
  (`m_2025_06_27`).
- `agent_servers.<name>.env`, `lsp.<name>.binary.env`, `dap.<name>.env`, `terminal.env`.
- `language_models.<provider>.custom_headers`, and `language_models.lmstudio.api_key`, a plaintext slot that the
  schema has and the reader does not use.
- `dev_container_connections`: an array whose elements carry `remote_env`, so the whole key goes.

Synced on purpose, with the secret scan as the backstop: `lsp.*.initialization_options`, `lsp.*.settings`, `proxy`
(the scan knows `scheme://user:pass@`), `context_servers.*.command` and `.args`.

## Noise: values that belong to one machine

Each entry carries its reason where it is listed. Font size is **not** noise: `Increase/Decrease*FontSize` has
`persist` false by default (`zed.rs`, the default keymap), so a size is a setting the user chose, not a side effect.

## What is not tracked

- `tasks.json`, `debug.json`: the root is an array and `env` sits inside its elements, so a key path cannot cut it.
- `prompts/` (LMDB, binary), `embeddings/`, `prompt_overrides/`, `*_backup.json`, `global_settings.json`,
  `git/config`.
- `AGENTS.md` (a follow-up candidate), a project's own `.zed/`, extensions, and a bundle check for Preview or Nightly
  alone.
- `~/.config/zed/development_credentials` is denied by construction (`system/deny_list.py`).
- `XDG_CONFIG_HOME` does not move the directory on macOS (`config_dir()`), and `--user-data-dir` is out of scope.
  `docs/src/remote-development.md` says `~/.zed`; the code and the other docs say `~/.config/zed`, and the code wins.

## Known risk, recorded not fixed

The migrator rewrites in memory and writes the file only when the user clicks "Backup and Update"
(`zed/src/zed/migrate.rs`); a write at startup was not seen (**UNVERIFIED**). If two Macs run different Zed
versions, a key format migrated on one reaches the older one.
"""

from __future__ import annotations

from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.tree import ANY_KEY, KeyPattern

SETTINGS = ".config/zed/settings.json"
KEYMAP = ".config/zed/keymap.json"
SNIPPETS = ".config/zed/snippets/*.json"
THEMES = ".config/zed/themes/*.json"

# Where an override can hold settings: the document root, each release channel, each platform, and each profile
# (`profiles.<name>.settings`). See "One policy, nine places".
PREFIXES: tuple[KeyPattern, ...] = (
    (),
    ("stable",),
    ("preview",),
    ("nightly",),
    ("dev",),
    ("macos",),
    ("linux",),
    ("windows",),
    ("profiles", ANY_KEY, "settings"),
)

# Credentials, relative to a prefix: an excluded key is in no object (``AGENTS.md`` §3.4).
EXCLUDED_RULES: tuple[KeyPattern, ...] = (
    ("context_servers", ANY_KEY, "env"),
    ("context_servers", ANY_KEY, "headers"),
    # `client_secret` for a pre-registered OAuth client.
    ("context_servers", ANY_KEY, "oauth"),
    # An extension MCP server's free-form settings, where a personal access token is typically put.
    ("context_servers", ANY_KEY, "settings"),
    # The legacy stdio shape (`m_2025_06_27`) keeps `env` under `command`.
    ("context_servers", ANY_KEY, "command", "env"),
    ("agent_servers", ANY_KEY, "env"),
    ("lsp", ANY_KEY, "binary", "env"),
    ("dap", ANY_KEY, "env"),
    ("language_models", ANY_KEY, "custom_headers"),
    # In the schema but not read: a plaintext slot nothing should ever fill.
    ("language_models", "lmstudio", "api_key"),
    ("terminal", "env"),
    # An array, and each element has `remote_env`: there is no key path inside it, so the whole key goes.
    ("dev_container_connections",),
)

# Machine-local, relative to a prefix.
NOISE_RULES: tuple[KeyPattern, ...] = (
    # Absolute paths to a Node install, which differ per machine.
    ("node", "path"),
    ("node", "npm_path"),
    # A language server binary and its arguments are where this machine installed it.
    ("lsp", ANY_KEY, "binary", "path"),
    ("lsp", ANY_KEY, "binary", "arguments"),
    ("dap", ANY_KEY, "binary"),
    ("dap", ANY_KEY, "args"),
    ("journal", "path"),
    ("git", "worktree_directory"),
    # Homebrew lives in `/usr/local` on Intel and `/opt/homebrew` on Apple silicon.
    ("terminal", "shell"),
    ("terminal", "working_directory", "always"),
    # "Allow always" makes Zed record an absolute path.
    ("agent", "sandbox_permissions"),
    # Trusting every worktree on one Mac must not spread to the others.
    ("session", "trust_all_worktrees"),
    ("cli_default_open_behavior",),
    ("feature_flags",),
    # Opening a remote project makes Zed add `projects` by itself, so syncing it conflicts on every sync.
    ("ssh_connections",),
    ("wsl_connections",),
)


def _under_every_prefix(rules: tuple[KeyPattern, ...]) -> frozenset[KeyPattern]:
    return frozenset((*prefix, *rule) for prefix in PREFIXES for rule in rules)


EXCLUDED: frozenset[KeyPattern] = _under_every_prefix(EXCLUDED_RULES)
NOISE: frozenset[KeyPattern] = _under_every_prefix(NOISE_RULES)

ZED = Preset.create(
    name="zed",
    files=(
        PresetFile.create(path=SETTINGS, format="jsonc", noise=NOISE, excluded=EXCLUDED),
        # The whole array is one value. `context` is optional and defaults to "", which does not fit the rule that
        # an absent field is not `""`; duplicate contexts are allowed and the later entry wins, so an element has
        # no identity to merge by. A per-block merge is a follow-up.
        PresetFile.create(path=KEYMAP, format="jsonc"),
        # No strip list: a snippet or a theme is the user's own text, and the secret scan is the backstop.
        PresetFile.create(path=SNIPPETS, format="jsonc"),
        PresetFile.create(path=THEMES, format="jsonc"),
    ),
    bundle_id="dev.zed.Zed",
    download_url="https://zed.dev/download",
    cask="zed",
)

__all__ = ["EXCLUDED", "KEYMAP", "NOISE", "PREFIXES", "SETTINGS", "SNIPPETS", "THEMES", "ZED"]
