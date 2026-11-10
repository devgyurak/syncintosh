# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `ghostty` preset: Ghostty's config files and themes, validated with Ghostty itself.

Step 0 evidence (inspected on a Mac, Ghostty 1.3.1, 2026-11-10 — the `add-preset` skill, Step 0-6):

```
App:       Ghostty 1.3.1        Bundle ID: com.mitchellh.ghostty   (/Applications/Ghostty.app)
Paths:     ~/.config/ghostty/config                  (in use: 26 keys, keybind x14, font-family x3)
           ~/.config/ghostty/config.ghostty          (absent here)
           ~/Library/Application Support/com.mitchellh.ghostty/config            (absent here)
           ~/Library/Application Support/com.mitchellh.ghostty/config.ghostty    (present, empty)
           ~/.config/ghostty/themes/*                (absent here; the theme search path is confirmed)
Order:     XDG `config`, then `config.ghostty` (the later file wins; verified with a temporary HOME).
           The App Support files use the real home rather than HOME, so their order could not be reproduced:
           the binary's documentation says macOS reads that location too, and nothing more is claimed.
           Which files are tracked does not depend on it.
Format:    `key = value` lines + `#` comments; a key may repeat and its order is meaningful -> text
Validator: <app>/Contents/MacOS/ghostty +validate-config --config-file=<path>   (rc 0 valid, 1 invalid)
Download:  https://ghostty.org/download (200)        Cask: ghostty
```

The format is `text`, not a keyed one: `keybind` and `font-family` are lists whose order matters, so a
key-path tree would lose or reorder entries. The merge is the line-level `MergeDriver`, and a both-sides
change to one line is a whole-file conflict. `noise`/`excluded` therefore stay empty (`PresetFile.create`
refuses them for `text`): a validator, not a strip list, keeps a merged config Ghostty would reject from being
published or written (`system/ghostty_check.py`).

`XDG_CONFIG_HOME` is not honoured (the same stance as `ZDOTDIR` for `zsh`), and a file pulled in by
`config-file = ...` is not followed: a user who wants it synced adds it as an entry of their own. The themes glob
is `~/.config/ghostty/themes/*` only, and leaves out hidden files — Ghostty does not read a `themes` directory
under Application Support.

**Machine-local values.** `text` cannot cut a line out, so a per-display value (`font-size`, `window-padding-*`,
`background-opacity`) is synced as written. To keep one local, put it in `config-file = ?local.ghostty` and do
not track that file (or track it with `scope = "machine"`): the `?` makes the include optional, so a machine
without the file still validates.

**Running Ghostty.** It never rewrites its own config files, so apply is not deferred while it runs. A theme and
a config that names it can arrive in one cycle and the config be refused once, because the theme file is not
there yet at the check; the next cycle applies it, so no ordering code exists for it.

**Credentials.** There is no strip list, so the secret scan is the only gate. The lines where a credential could
sit are `env = ...`, `command = ...` and `initial-command = ...`: a token passed to a launched program lives
there, and nothing but the scan keeps it out of the repo. The scan is a heuristic: it recognises vendor-shaped
tokens (a GitHub token, a bearer header) and not a generic `KEY=value` or `--password` argument, so a credential
belongs in the environment of the launched program, never on one of those lines.
"""

from __future__ import annotations

from syncintosh.core.presets import Preset, PresetFile

# The files Ghostty reads, XDG first. `config.ghostty` is the newer name and loads after `config`, so a value
# moved between the two would look like a deletion if only one were tracked.
CONFIG_FILES: tuple[str, ...] = (
    ".config/ghostty/config",
    ".config/ghostty/config.ghostty",
    "Library/Application Support/com.mitchellh.ghostty/config",
    "Library/Application Support/com.mitchellh.ghostty/config.ghostty",
)
THEMES = ".config/ghostty/themes/*"

GHOSTTY = Preset.create(
    name="ghostty",
    files=(
        *(PresetFile.create(path=path, format="text", validator="ghostty") for path in CONFIG_FILES),
        # A theme is the user's own text in the same syntax, so it takes the same check. A glob leaves hidden
        # files out (`PresetGlob`): Finder drops a `.DS_Store` (not text) into any folder it opens, and a validator
        # check killed part-way leaves a hidden `.<name>.<hex>.syncintosh-check` beside its target.
        PresetFile.create(path=THEMES, format="text", validator="ghostty"),
    ),
    bundle_id="com.mitchellh.ghostty",
    download_url="https://ghostty.org/download",
    cask="ghostty",
)

__all__ = ["CONFIG_FILES", "GHOSTTY", "THEMES"]
