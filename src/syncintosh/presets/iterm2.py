# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `iterm2` preset: iTerm2's preferences domain and Dynamic Profiles, merged one profile at a time.

Step 0 evidence (inspected on a Mac, iTerm2 3.7.7, source read at tag `v3.7.7` of `gnachman/iTerm2`, 2026-11-10 —
the `add-preset` skill, Step 0-6; the full write-up is on issue #58):

```
App:       iTerm2 3.7.7         Bundle ID: com.googlecode.iterm2   (/Applications/iTerm.app)
Paths:     ~/Library/Application Support/iTerm2/DynamicProfiles/*.json
           com.googlecode.iterm2  (the preferences domain; ~/Library/Preferences/com.googlecode.iterm2.plist)
Format:    domain: an XML plist (`defaults export`), profiles in `New Bookmarks`; Dynamic Profile: JSON `Profiles`
           array; every entry needs a `Guid` and a name
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

**The preferences domain.** `cfprefsd` owns the domain: it caches the values and rewrites the plist on its own
schedule, so the file is never read or written here (it is on the deny list). The domain is presented as one virtual
file, `Library/Preferences/@defaults/com.googlecode.iterm2.plist`, that `system/defaults_domains.py` serves through
`defaults export` and `defaults import` (`core/documents.py`). `import` merges per top-level key, so the store also
deletes the keys a document no longer has; see the store's docstring for what was observed. The view is the canonical
XML plist (`core/plist_xml.py`), merged by the `plist` adapter, and `New Bookmarks` is an identified array like
`Profiles`: two entries are the same profile when they share a `Guid`.

**An allowlist, not a deny-list.** The domain also holds window frames, migration markers, Sparkle state and keys
AppKit writes into every app's domain. Only the keys in `SYNCED_NAMES` travel: the preferences iTerm2 registers
(`iTermPreferences.m`, `+defaultValueMap`, 208 keys, snapshotted in `tests/fixtures/iterm2/registered_keys.txt`)
minus the ones `LOCAL` gives a reason for, plus `New Bookmarks`, `Default Bookmark Guid` and `GlobalKeyMap`. A key
iTerm2 adds later stays local until this preset names it. iTerm2's own sync-exempt rule
(`iTermRemotePreferencesKeyIsSyncable`: the prefixes `NS`, `SU`, `NoSync`, `UK` and a short list of names) is
honoured in full, and a test pins that every key seen on the inspected machine is synced, excluded or local.

**Credentials.** iTerm2 keeps the AI API keys in the Keychain (service `iTerm2 API Keys`) and the password manager in
the Keychain or the 1Password and LastPass tools; none of that is in the domain. The three keys in `EXCLUDED_NAMES`
are the ones that could hold a secret in the domain itself (the deprecated plaintext `NoSyncOpenAIAPIKey`, the
`NoUserDefaultAIAPIKey` placeholder, `AICustomHeaders` which can carry a bearer token); the AI keys are local as a
whole. A profile is the user's own text: `Command`, `Custom Command`, `Initial Text`, trigger parameters, `Keyboard
Map` send-text actions and snippets are where a credential could be typed, and **the secret scan is the only gate for
them** (a heuristic, not a guarantee). `Working Directory` and `Custom Directory` are synced as written, so a path under
a home folder that differs between Macs is a profile edit to make once.

**Open design points.** A fresh install gives its default profile a `Guid` of its own, so two Macs that never shared
settings hold two "Default" profiles and a `Default Bookmark Guid` that differ: the merge keeps both profiles and the
default key is a both-sides change. It is a one-time choice to make, and the key is synced as designed.

**Not covered here.** `dynamicProfilesPath` and a Dynamic Profile written as a plist.

**When iTerm2 is open.** The whole preset waits for it to quit (`defer_while_running`), and so does `running` being
unknowable: a running iTerm2 holds the domain in memory and rewrites it on its own schedule, so a write from outside
is lost or fights it. Dynamic Profiles are reloaded live and could be written meanwhile, but the preset is one unit,
so they wait too. Collect keeps flowing: it only reads.

**When iTerm2 loads its own settings.** With `LoadPrefsFromCustomFolder` on, iTerm2 loads and saves the plist in its
custom folder itself, a second sync channel that would fight this one. The domain is then skipped in collect and apply,
and so is a value that is not plainly off or cannot be read (`skip_reason`); the report says why.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from syncintosh.core.documents import CFPREFS_DOMAIN, DocumentSource
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.tree import KeyPath, KeyPattern

BUNDLE_ID: Final = "com.googlecode.iterm2"
DOMAIN: Final = "com.googlecode.iterm2"
DATABASE: Final = "Library/Preferences/com.googlecode.iterm2.plist"
# The `@defaults` folder is virtual: no real folder is called that, so the path cannot collide with a file on disk.
VIEW: Final = "Library/Preferences/@defaults/com.googlecode.iterm2.plist"
DYNAMIC_PROFILES: Final = "Library/Application Support/iTerm2/DynamicProfiles/*.json"

# Prefixes iTerm2 itself never syncs (`iTermRemotePreferencesKeyIsSyncable`), plus `Apple`, which AppKit writes into
# every app's domain. A key under one is local without being listed.
LOCAL_PREFIXES: Final = ("NS", "SU", "NoSync", "UK", "Apple")

SYNCED_NAMES: tuple[str, ...] = (
    "AddNewTabAtEndOfTabs",
    "AdjustWindowForFontSizeChange",
    "Alert On Marks in Offscreen Sessions",
    "AllowSymbolicHotKeys",
    "AlwaysOpenWindowAtStartup",
    "AutoHideTmuxClientSession",
    "Bidi",
    "BracketedPasteMode",
    "CheckTestRelease",
    "ClickToSelectCommand",
    "CommandSelection",
    "Control",
    "ConvertDosNewlines",
    "ConvertUnicodePunctuation",
    "CopyLastNewline",
    "CopySelection",
    "Default Toolbelt Width",
    "DimBackgroundWindows",
    "DimInactiveSplitPanes",
    "DimOnlyText",
    "DisableFullscreenTransparency",
    "DisableTransparencyForKeyWindow",
    "DoubleClickPerformsSmartSelection",
    "EnableDivisionView",
    "EnableProxyIcon",
    "EnableRendezvous",
    "EnterCopyModeAutomatically",
    "EscapeShellCharsWithBackslash",
    "FFM Including Subviews",
    "FlashTabBarInFullscreen",
    "FocusFollowsMouse",
    "FocusOnRightOrMiddleClick",
    "ForceKeyboard",
    "ForceKeyboardOncePerSession",
    "FunctionKey",
    "HTMLTabTitles",
    "HapticFeedbackForEsc",
    "HideActivityIndicator",
    "HideFromDockAndAppSwitcher",
    "HideMenuBarInFullscreen",
    "HideScrollbar",
    "HideTab",
    "HideTabCloseButton",
    "HideTabNumber",
    "HotKeyBookmark",
    "HotKeyTogglesWindow",
    "Hotkey",
    "HotkeyAutoHides",
    "HotkeyChar",
    "HotkeyCode",
    "HotkeyMigratedFromSingleToMulti",
    "HotkeyModifiers",
    "IRMemory",
    "JobName",
    "LanguageAgnosticKeyBindings",
    "LeftCommand",
    "LeftControl",
    "LeftOption",
    "LeftTabBarWidth",
    "MaxVertically",
    "MenuBarItemString",
    "NeverBlockSystemShutdown",
    "NotifyOnlyForCriticalShellIntegrationUpdates",
    "NumberOfSpacesPerTab",
    "OnlyWhenMoreTabs",
    "OpenArrangementAtStartup",
    "OpenBookmark",
    "OpenNoWindowsAtStartup",
    "OpenQuicklyHotkeyChar",
    "OpenQuicklyHotkeyCode",
    "OpenQuicklyHotkeyModifiers",
    "OpenTmuxWindowsIn",
    "OptionClickMovesCursor",
    "PassOnControlClick",
    "PasteSpecialChunkDelay",
    "PasteSpecialChunkSize",
    "PasteTabToStringTabStopSize",
    "PerPaneBackgroundImage",
    "PhonyOpenTmuxWindowsAsTabsInAttachingWindow",
    "PhonyOpenUnrecognizedTmuxWindowsIn",
    "PreserveWindowSizeToKeepOnScreenWhenTabBarVisibilityChanges",
    "PreserveWindowSizeWhenTabBarVisibilityChanges",
    "PromptOnQuit",
    "PromptOnQuitEvenIfThereAreNoWindows",
    "QuitWhenAllWindowsClosed",
    "RemapModifiersGlobally",
    "RememberWindowPositions",
    "RemoveControlCodes",
    "ReportHorizontalScrollEvents",
    "ReportRightClick",
    "RestoreWindowsToSameSpaces",
    "RightCommand",
    "RightControl",
    "RightOption",
    "SavePasteHistory",
    "ScrollableSideTabBar",
    "SeparateStatusBarsPerPane",
    "SeparateWindowTitlePerTab",
    "ShowBookmarkName",
    "ShowFullScreenTabBar",
    "ShowMenuBarItem",
    "ShowNewOutputIndicator",
    "ShowPaneTitles",
    "ShowPaneTitlesEvenIfOnlyOnePane",
    "Size Changes Affect Profile",
    "SmartPlacement",
    "SoundForEsc",
    "SplitPaneDimmingAmount",
    "SshIntegrationForURLs",
    "StatusBarPosition",
    "StretchTabsToFillBar",
    "SwitchPaneModifier",
    "SwitchTabModifier",
    "SwitchWindowModifier",
    "TabStyle",
    "TabStyleWithAutomaticOption",
    "TabTransform",
    "TabViewType",
    "TabsHaveCloseButton",
    "TerminalMargin",
    "TerminalVMargin",
    "ThreeFingerEmulates",
    "TmuxDashboardLimit",
    "TmuxPauseModeAgeLimit",
    "TmuxSyncClipboard",
    "TmuxUnpauseAutomatically",
    "TmuxUsesDedicatedProfile",
    "TmuxWarnBeforePausing",
    "TripleClickSelectsFullWrappedLines",
    "UIElementRequiresHotkeys",
    "UseBorder",
    "UseLionStyleFullscreen",
    "UseTmuxStatusBar",
    "UseVirtualKeyCodesForDetectingDigits",
    "VisualIndicatorForEsc",
    "WindowNumber",
    "WindowPlacement",
    "WordCharacters",
    "WordSelectionMode",
    "WordSelectionRegexPattern",
    "WrapDroppedFilenamesInQuotesWhenPasting",
    # The profile list, which profile is the default, and the global key map: what makes a terminal feel the same.
    "New Bookmarks",
    "Default Bookmark Guid",
    "GlobalKeyMap",
)

EXCLUDED_NAMES: tuple[str, ...] = (
    "AICustomHeaders",
    "NoSyncOpenAIAPIKey",
    "NoUserDefaultAIAPIKey",
)

LOCAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "AI provider setup: its key is in the Keychain, so the choice alone would leave it half-set": frozenset(
            {
                "AI Prompt",
                "AI Prompt for AI Chat Orchestration",
                "AI Prompt for AI Chat with Browser",
                "AI Prompt for AI Chat with ReadOnlyTerminal",
                "AI Prompt for AI Chat with ReadOnlyTerminalBrowser",
                "AI Prompt for AI Chat with ReadWriteTerminal",
                "AI Prompt for AI Chat with ReadWriteTerminalBrowser",
                "AI Prompt for AI Chat with no function calling",
                "AI Prompt for Chat Icon",
                "AI Prompt for Code Review",
                "AI Prompt for Code Review System",
                "AI Safety Check",
                "AI Timeout",
                "AICompletion",
                "AICustomHeadersEnabled",
                "AIEconomyModelName",
                "AIFeatureFunctionCalling",
                "AIFeatureHostedCodeInterpeter",
                "AIFeatureHostedFileSearch",
                "AIFeatureHostedWebSearch",
                "AIFeatureStreamingResponses",
                "AIManualModelConfigurations",
                "AIModelUpdatesEnabled",
                "AIOllamaEconomyModel",
                "AIOllamaRegularModel",
                "AIPermissionActInWebBrowser",
                "AIPermissionCheckTerminalState",
                "AIPermissionRunCommands",
                "AIPermissionTypeForYou",
                "AIPermissionViewHistory",
                "AIPermissionViewManpages",
                "AIPermissionWriteToClipboard",
                "AIPermissionWriteToFilesystem",
                "AITermAPI",
                "AIVectorStore",
                "AIVendor",
                "AIZeroDataRetention",
                "API Authentication Method",
                "AiMaxTokens",
                "AiModel",
                "AiResponseMaxTokens",
                "AitermURL",
                "AitermUseLegacyAPI",
                "Enable AI",
                "NoUserDefaultAIPromptPlaceholder",
                "UseRecommendedAIModel",
            }
        ),
        "a permission: widening what a program may do is not synced as a side effect": frozenset(
            {
                "AllowClipboardAccess",
                "EnableAPIServer",
                "PhonyAllowSendingClipboardContents",
            }
        ),
        "a folder on this Mac": frozenset(
            {
                "CustomScriptsFolder",
                "UseCustomScriptsFolder",
            }
        ),
        "this Mac's GPU or keyboard layout": frozenset(
            {
                "KeyboardLocale",
                "UseMetal",
                "disableMetalInLowPowerMode",
                "disableMetalWhenUnplugged",
                "metalMaximizeThroughput",
                "preferIntegratedGPU",
            }
        ),
        "iTerm2's own sync-exempt rule, or a key that never leaves the app's own side": frozenset(
            {
                "LoadPrefsFromCustomFolder",
                "NSMenuEnableActionImages",
                "NoSyncMigratedDynamicProfileTagToFlag",
                "NoSyncNeverRemindPrefsChangesLostForFile",
                "NoSyncNeverRemindPrefsChangesLostForFile_selection",
                "PrefsCustomFolder",
                "SUEnableAutomaticChecks",
            }
        ),
    }
)

# Observed on the inspected machine and not registered by iTerm2: AppKit's own keys (`Apple*`, `NS*`) and the app's
# version stamp. Listed so a test can pin that every observed key has a reason; the prefixes above cover the rest.
OBSERVED_LOCAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "the app's own version stamp, which iTerm2 never syncs": frozenset({"iTerm Version"}),
    }
)

SYNCED: frozenset[KeyPath] = frozenset((name,) for name in SYNCED_NAMES)
EXCLUDED: frozenset[KeyPattern] = frozenset((name,) for name in EXCLUDED_NAMES)

SOURCE = DocumentSource.create(
    kind=CFPREFS_DOMAIN,
    database=DATABASE,
    domain=DOMAIN,
    view=VIEW,
    skip_when="LoadPrefsFromCustomFolder",
    skip_because=(
        "iTerm2 is set to load its preferences from a custom folder, so it manages them itself; "
        "turn that off in Settings > General > Settings to sync them here"
    ),
)

# An entry is its `Guid`, the field iTerm2 itself keys a profile by.
PROFILE_RULE = ArrayRule.create(identity=("Guid",), required=("Guid",), group=("Guid",))

ITERM2 = Preset.create(
    name="iterm2",
    files=(
        PresetFile.create(
            path=VIEW,
            format="plist",
            synced=SYNCED,
            excluded=EXCLUDED,
            array_identity={("New Bookmarks",): PROFILE_RULE},
        ),
        # A glob with a literal part (`*.json`) also takes in a dot-leading name, which iTerm2 itself skips: such a
        # file is synced but never loaded.
        PresetFile.create(path=DYNAMIC_PROFILES, format="jsonc", array_identity={("Profiles",): PROFILE_RULE}),
    ),
    bundle_id=BUNDLE_ID,
    download_url="https://iterm2.com/downloads.html",
    cask="iterm2",
    defer_while_running=True,
    documents=(SOURCE,),
)

__all__ = [
    "BUNDLE_ID",
    "DATABASE",
    "DOMAIN",
    "DYNAMIC_PROFILES",
    "EXCLUDED",
    "EXCLUDED_NAMES",
    "ITERM2",
    "LOCAL",
    "LOCAL_PREFIXES",
    "SOURCE",
    "SYNCED",
    "SYNCED_NAMES",
    "VIEW",
]
