# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `iterm2` preset: iTerm2 Dynamic Profiles (`*.json`), merged one profile at a time.

Step 0 evidence (inspected on a Mac, iTerm2 3.7.7, source read at tag `v3.7.7` of `gnachman/iTerm2`, 2026-11-10 —
the `add-preset` skill, Step 0-6; the full write-up is on issue #58):

```
App:       iTerm2 3.7.7         Bundle ID: com.googlecode.iterm2   (/Applications/iTerm.app)
Paths:     ~/Library/Application Support/iTerm2/DynamicProfiles/*.json
Format:    JSON object with a `Profiles` array; every entry needs a `Guid` and a name
Download:  https://iterm2.com/downloads.html (200)        Cask: iterm2
```

**What iTerm2 reads** (`iTermDynamicProfileManager.m`). Every file in the directory except one whose name starts with
`.` or ends with `~`; it tries a property list first and JSON second, so the extension means nothing to iTerm2. This
preset tracks `*.json` only, and a Dynamic Profile written as a plist is out of scope. The root must be an object with a
`Profiles` array; an entry without a `Guid` or a name is refused by iTerm2, a `Guid` repeated across files is an error,
and so is one equal to an ordinary profile's. A path watcher reloads on change, and iTerm2 itself may rewrite a
Dynamic Profile file, so the file is never assumed to be ours alone. The advanced setting `dynamicProfilesPath` can move
the directory; that is not followed (the same stance as `ZDOTDIR` for `zsh`).

**Merge.** `Profiles` is an identified array: two entries are the same profile when they share a `Guid`. A profile one
machine added and the other did not is kept; a profile both machines changed differently is a conflict on that profile
only (`core/keyed_array.py`, the rule VS Code keybindings use). iTerm2 gives no ordering meaning to the array.

**No strip list.** A Dynamic Profile is the user's own text, so nothing is stripped. A profile's `Command`, `Custom
Command`, `Initial Text` and trigger parameters are where a credential could be typed, and the secret scan is the only
gate for them (the scan is a heuristic, not a guarantee).

**Not covered here.** The main preferences domain (`com.googlecode.iterm2`) is a separate change, and
`defer_while_running` is not set: a Dynamic Profiles file is reloaded live, so apply need not wait for iTerm2 to quit.
"""

from __future__ import annotations

from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.presets import Preset, PresetFile

DYNAMIC_PROFILES = "Library/Application Support/iTerm2/DynamicProfiles/*.json"

ITERM2 = Preset.create(
    name="iterm2",
    files=(
        # An entry is its `Guid`, the field iTerm2 itself keys a profile by. A glob with a literal part (`*.json`)
        # also takes in a dot-leading name, which iTerm2 itself skips: such a file is synced but never loaded.
        PresetFile.create(
            path=DYNAMIC_PROFILES,
            format="jsonc",
            array_identity={("Profiles",): ArrayRule.create(identity=("Guid",), required=("Guid",), group=("Guid",))},
        ),
    ),
    bundle_id="com.googlecode.iterm2",
    download_url="https://iterm2.com/downloads.html",
    cask="iterm2",
)

__all__ = ["DYNAMIC_PROFILES", "ITERM2"]
