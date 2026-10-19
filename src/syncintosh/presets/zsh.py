# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `zsh` preset: the five startup files, validated with `zsh -fn`.

Step 0 evidence (inspected on a Mac, 2026-10 — the `add-preset` skill, Step 0-6):

```
App:       zsh 5.9 (macOS built-in)      Bundle ID / Download / Cask: — (ships with macOS)
Paths:     ~/.zshenv  ~/.zprofile  ~/.zshrc  ~/.zlogin  ~/.zlogout
Format:    text (line-level three-way merge)
Validator: zsh -fn  (syntax only, rc files suppressed)
Noise:     none — a startup file has no machine-specific key to strip
Excluded:  none in code; the secret scan is the backstop for a token inlined in a file
Symlink:   ~/.zshrc → ~/dotfiles/zshrc is not followed and is reported, not written through
           (docs/design/03-manifest.md, "Open")
```

Only the **five files zsh actually reads at startup** are listed. `ZDOTDIR` is not honoured:
a user who moves their startup files with it names paths this preset does not know, and the
apply-side catalog check refuses them rather than guessing (``docs/design/03-manifest.md``).

The format is `text`, not a keyed format, because a shell script has no key paths — the merge
is the line-level `MergeDriver`, and a both-sides change to one line is a whole-file conflict.
`noise`/`excluded` therefore stay empty: a validator, not a strip list, is what keeps a merged
`.zshrc` from being published with a syntax error.
"""

from __future__ import annotations

from syncintosh.core.presets import Preset, PresetFile

# The files zsh reads, in the order it reads them. `~/.zshrc` is the one most users edit; the
# others must be tracked too, or a value moved between them would look like a deletion.
STARTUP_FILES: tuple[str, ...] = (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zlogout")

ZSH = Preset.create(
    name="zsh",
    files=tuple(PresetFile.create(path=path, format="text", validator="zsh") for path in STARTUP_FILES),
    bundle_id=None,
    download_url=None,
    cask=None,
)

__all__ = ["STARTUP_FILES", "ZSH"]
