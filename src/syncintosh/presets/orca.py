# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `orca` preset: the preference keys of one `settings` row in Orca's profile database.

**Evidence: inspected on a real machine, 2026-11-09** (Orca 1.4.219; the key names came from the masking inspector and
no value was copied anywhere; issue #55, https://github.com/devgyurak/syncintosh/issues/55#issuecomment-6734812135):

```
App:       Orca 1.4.219 (Stably AI), /Applications/Orca.app, Electron
Bundle ID: com.stablyai.orca
Store:     ~/Library/Application Support/orca/profiles/local-default/profile-state.db   (SQLite, WAL, user_version 3)
           profile_state_documents row domain='settings', domain_version 1, JSON payload (208 top-level keys here)
           orca-data.json beside it is a stale compatibility export: never read, never written
Profile:   `local-default` is a constant in Orca's code (the default "Personal" profile); others get another id
Format:    JSON, shown to the sync as an indented virtual file (`core/documents.py`), written back compact
Keybinds:  not in `settings`; a user file `~/.orca/keybindings.json` (absent on the inspected machine, not tracked)
Noise:     `notifications.customSoundPath` (an absolute path)
Excluded:  every credential-shaped key below, in code
Process:   the app and `orcad` (`Orca Helper … daemon-entry.js`, pid in `daemon/daemon-v<N>.pid`), both inside Orca.app
Download:  https://github.com/stablyai/orca/releases   (the update feed the bundle names: `app-update.yml`)   cask: none
```

Homebrew's `orca` cask is plotly's unrelated app, so `cask` is `None`. The bundle's own `app-update.yml` says
`provider: github, owner: stablyai, repo: orca`; that is the only download location the bundle itself names.

## An allowlist, not a deny-list

Orca adds settings in nearly every release (`…Migrated`, `…DefaultedForAllUsers`, `experimental…`). A deny-list would
start moving each new key, credential-shaped ones included, until someone noticed; an allowlist leaves a key local
until this module is updated. So `SYNCED` names the exact top-level keys that travel, and **everything else stays on
the machine** (`PresetFile.synced`, `core/projection.py`): it is cut before sealing and put back from the live row on
apply. `LOCAL` records why each key seen on the inspected machine is not in `SYNCED`; a test pins that every observed
key is classified as synced, local or excluded, so a reviewer sees a decision and not an accident.

## Excluded: credentials, and values that hold one

Named in code as well (`EXCLUDED`), although they are not in `SYNCED`, so a later edit that moved one into the
allowlist is refused at construction (`PresetFile.create`) instead of leaking:

- `opencodeSessionCookie`, `opencodeGoApiKey`, `opencodeWorkspaceId`, `minimaxGroupId`, `browserKagiSessionLink`
- `codexManagedAccounts`, `activeCodexManagedAccountId`, `activeCodexManagedAccountIdsByRuntime`,
  `claudeManagedAccounts`, `activeClaudeManagedAccountId`
- `agentDefaultEnv`, `agentCmdOverrides`, `nativeChatShellEnvironmentVariables`, `nativeChatSessionOptions`
- `httpProxyUrl` (may embed `user:pass@`; Orca stores some such values sealed with the OS secret store, which is
  ciphertext bound to this machine and useless on another)
- `mobilePairingCustomAddress`, `mobilePairingCustomAddresses`

**Not verified:** which settings Orca seals at rest. The bundle's secret-store modules protect credential *files*
(Linear, Jira, speech); the list of sealed keys inside the `settings` row was not found. The allowlist is therefore
chosen from plain preference values (booleans, numbers, enums, font and theme names), and no key whose value could
be a secret is in it.

## Left to the user, on purpose

`agentDefaultArgs`, `terminalQuickCommands` and `openInApplications` hold command lines, which can carry a token or
run something on a Mac the user did not mean; they stay local until a follow-up decides how to carry them.
`terminalAllowOsc52Clipboard`, `agentWorkspaceTrustEnabled` and `agentStatusHooksEnabled` are permissions: a
setting that widens what a program may do is not carried to another Mac as a side effect of syncing a theme.

## Running

Apply defers while Orca **or `orcad`** runs, and when that cannot be told (`defer_while_running`, and
`system/orca_daemon.py`): a running Orca keeps the revision it last saw, so a write from outside stops its saving
("Saving has stopped. Restart Orca"). Collect keeps flowing: it only reads. Only the `local-default` profile is
supported (issue #55, D9).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from syncintosh.core.documents import ORCA_PROFILE_STATE, DocumentSource
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.tree import KeyPath, KeyPattern

BUNDLE_ID: Final = "com.stablyai.orca"

PROFILE_DIRECTORY: Final = "Library/Application Support/orca/profiles/local-default"
DATABASE: Final = f"{PROFILE_DIRECTORY}/profile-state.db"
# The `@` folder is virtual: no real folder is called that, so the path cannot collide with a file on disk.
VIEW: Final = f"{PROFILE_DIRECTORY}/@profile-state/settings.json"

# Preferences that travel, by exact top-level key. Plain values only: no path, host, command line, token or id.
SYNCED: frozenset[KeyPath] = frozenset(
    (key,)
    for key in (
        # Workspace and appearance.
        "worktreeVisibilityDefaults",
        "nestWorkspaces",
        "refreshLocalBaseRefOnWorktreeCreate",
        "autoRenameBranchFromWork",
        "branchPrefix",
        "branchPrefixCustom",
        "theme",
        "leftSidebarAppearanceMode",
        "leftSidebarTintColor",
        "leftSidebarTintOpacity",
        "uiLanguage",
        "appIcon",
        "appFontFamily",
        "windowBackgroundBlur",
        "minimizeToTrayOnClose",
        "showMenuBarIcon",
        # Editor and Markdown.
        "editorAutoSave",
        "editorAutoSaveDelayMs",
        "editorMinimapEnabled",
        "editorFontFamily",
        "editorWordWrap",
        "editorPreviewTabsEnabled",
        "richMarkdownSpellcheckEnabled",
        "markdownReviewToolsEnabled",
        # Terminal.
        "primarySelectionMiddleClickPaste",
        "terminalFontSize",
        "terminalFontFamily",
        "terminalFontWeight",
        "terminalFontWeightBold",
        "terminalLineHeight",
        "terminalScrollSensitivity",
        "terminalFastScrollSensitivity",
        "terminalTuiScrollSensitivity",
        "terminalGpuAcceleration",
        "terminalLigatures",
        "terminalInlineImages",
        "terminalCursorStyle",
        "terminalCursorBlink",
        "terminalThemeDark",
        "terminalDividerColorDark",
        "terminalUseSeparateLightTheme",
        "terminalThemeLight",
        "terminalCustomThemes",
        "terminalDividerColorLight",
        "terminalInactivePaneOpacity",
        "terminalActivePaneOpacity",
        "terminalPaneOpacityTransitionMs",
        "terminalDividerThicknessPx",
        "terminalRightClickToPaste",
        "terminalMouseHideWhileTyping",
        "terminalFocusFollowsMouse",
        "terminalClipboardOnSelect",
        "terminalCopyTrimsGutter",
        "terminalScrollbackRows",
        "terminalLinkActionPopoverEnabled",
        "terminalLinkClickBehavior",
        "terminalUrlMiddleClickBehavior",
        "terminalMacOptionAsAlt",
        "terminalJISYenToBackslash",
        "terminalShortcutPolicy",
        "terminalScopeHistoryByWorktree",
        # Links, panels and source control views.
        "openLinksInApp",
        "openLinksInAppModifierInverts",
        "localhostWorktreeLabelsEnabled",
        "rightSidebarOpenByDefault",
        "showGitIgnoredFiles",
        "sourceControlViewMode",
        "sourceControlGroupOrder",
        "sourceControlCompareAgainstUpstream",
        "showTitlebarAppName",
        "showTasksButton",
        "showAutomationsButton",
        "showArtifactsButton",
        "showSkillsButton",
        "showMobileButton",
        "showPinnedWorktreesInGroups",
        "ctrlTabOrderMode",
        "tabAutoGenerateTitle",
        "confirmClosePinnedTab",
        "compactWorktreeCards",
        # Diff.
        "diffDefaultView",
        "diffWordWrap",
        "diffShowWhitespace",
        "diffCollapseUnchangedRegions",
        "combinedDiffFileTreeVisibleByDefault",
        # Notifications (a custom sound's path is noise below).
        "notifications",
        # Agents and tasks: which agent is the default and which are off, not how it is launched.
        "defaultTuiAgent",
        "disabledTuiAgents",
        "claudeAgentTeamsMode",
        "setupScriptLaunchMode",
        "defaultTaskViewPreset",
        "defaultTaskSource",
        "visibleTaskProviders",
        "keepComputerAwakeWhileAgentsRun",
        "computerAwakeMode",
        "promptCacheTimerEnabled",
        "promptCacheTtlMs",
    )
)

# Credential-shaped keys, named even though none is in `SYNCED`: moving one into the allowlist is refused by
# `PresetFile.create`, and the object holds none of them either way (``AGENTS.md`` §3.4).
EXCLUDED: frozenset[KeyPattern] = frozenset(
    (key,)
    for key in (
        "opencodeSessionCookie",
        "opencodeGoApiKey",
        "opencodeWorkspaceId",
        "minimaxGroupId",
        "browserKagiSessionLink",
        "codexManagedAccounts",
        "activeCodexManagedAccountId",
        "activeCodexManagedAccountIdsByRuntime",
        "claudeManagedAccounts",
        "activeClaudeManagedAccountId",
        "agentDefaultEnv",
        "agentCmdOverrides",
        "nativeChatShellEnvironmentVariables",
        "nativeChatSessionOptions",
        "httpProxyUrl",
        "mobilePairingCustomAddress",
        "mobilePairingCustomAddresses",
    )
)

# Machine-local inside a synced key. A custom notification sound is an absolute path to a file on this Mac.
NOISE: frozenset[KeyPattern] = frozenset({("notifications", "customSoundPath")})

# Why a key seen on the inspected machine is **not** carried. Grouped by reason, and pinned by a test: every observed
# key is synced, excluded or listed here.
LOCAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "an absolute path, or a list of them": frozenset(
            {
                "workspaceDir",
                "workspaceDirHistory",
                "floatingTerminalCwd",
                "floatingTerminalTrustedCwds",
                "androidSdkPath",
                "devPluginPaths",
                "terminalDefaultShell",
            }
        ),
        "a migration marker or a 'defaulted' flag this machine's own history decides": frozenset(
            {
                "autoRenameBranchFromWorkDefaultedOn",
                "primarySelectionMiddleClickPasteDefaultedForLinux",
                "primarySelectionMiddleClickPasteDefaultedForTerminalDefaults",
                "terminalTuiScrollSensitivityDefaultedToOne",
                "terminalCursorStyleDefaultedToBlock",
                "terminalRightClickToPasteDefaultedForPlatform",
                "localAccountRuntimeDefaultedToAutoForAllUsers",
                "terminalAllowOsc52ClipboardDefaultedOnForAllUsers",
                "floatingTerminalDefaultedForAllUsers",
                "floatingTerminalCwdMigratedToAppWorkspace",
                "claudeAgentTeamsDefaultDisabledMigrated",
                "agentYoloDefaultsMigrated",
                "terminalMacOptionAsAltMigrated",
                "experimentalActivityDefaultedOffForAllUsers",
                "visibleTaskProvidersDefaultedForJira",
                "agentsSidebarMigratedFromExperimental",
                "tabSwitchKeybindingSeed",
            }
        ),
        "a dismissed or already-asked prompt": frozenset(
            {
                "localBaseRefSuggestionDismissed",
                "openLinksInAppPreferencePrompted",
                "skipDeleteWorktreeConfirm",
                "skipCloseTerminalWithRunningProcessConfirm",
                "skipDeleteAutomationConfirm",
                "skipDeleteArtifactConfirm",
                "skipCodexRateLimitResetConfirm",
                "codexSharedServerWarning",
            }
        ),
        "an experimental or per-machine feature flag": frozenset(
            {
                "experimentalNativeChat",
                "experimentalStructuredNativeChat",
                "nativeChatResumeWorkOnRestart",
                "nativeChatQueueFollowUps",
                "nativeChatInheritShellEnvironment",
                "openAgentTabsInChatByDefault",
                "experimentalMobile",
                "experimentalPet",
                "experimentalActivity",
                "experimentalTerminalAttention",
                "experimentalAgentHibernation",
                "agentHibernationIdleMs",
                "experimentalNewWorktreeCardStyle",
                "experimentalEphemeralVms",
                "pluginSystemEnabled",
                "disabledPlugins",
                "pluginConsents",
                "artifactsEnabled",
                "artifactSharingEnabled",
                "agentSkillSharingEnabled",
                "nestedWorkerMaxDepth",
                "floatingTerminalEnabled",
                "floatingTerminalTriggerLocation",
                "browserClientHostedRemoteEnabled",
                "mobileEmulatorEnabled",
                "mobileEmulatorDefaultDeviceUdid",
                "mobileAutoRestoreFitMs",
                "mobilePairingConnectionMode",
                "terminalHiddenViewParking",
                "terminalSshViewParking",
                "terminalHiddenWorktreeRetentionBudget",
                "browserGuestWorktreeRetentionBudget",
                "terminalMainSideEffectAuthority",
                "terminalHiddenDeliveryGate",
                "terminalModelQueryAuthority",
                "codexTerminalServerIsolation",
                "electronHttp1CompatibilityMode",
                "geminiCliOAuthEnabled",
            }
        ),
        "a command line, which can carry a token or run something on another Mac": frozenset(
            {"agentDefaultArgs", "terminalQuickCommands", "openInApplications"}
        ),
        "a permission: widening what a program may do is not synced as a side effect": frozenset(
            {"terminalAllowOsc52Clipboard", "agentWorkspaceTrustEnabled", "agentStatusHooksEnabled"}
        ),
        "this machine's identity, runtime or network, or a subtree of machine state": frozenset(
            {
                "machineName",
                "telemetry",
                "activeRuntimeEnvironmentId",
                "localAccountRuntime",
                "localAccountWslDistro",
                "localWindowsRuntimeDefault",
                "terminalWindowsShell",
                "terminalWindowsWslDistro",
                "terminalWindowsPowerShellImplementation",
                "httpProxyBypassRules",
                "prBotAuthorOverrides",
                "defaultRepoSelection",
                "defaultLinearTeamSelection",
                "githubProjects",
                "commitMessageAi",
                "sourceControlAi",
                "voice",
                "minimaxUsageModels",
                "minimaxEndpoint",
            }
        ),
    }
)

SOURCE = DocumentSource.create(kind=ORCA_PROFILE_STATE, database=DATABASE, domain="settings", view=VIEW)

ORCA = Preset.create(
    name="orca",
    files=(PresetFile.create(path=VIEW, format="jsonc", synced=SYNCED, excluded=EXCLUDED, noise=NOISE),),
    bundle_id=BUNDLE_ID,
    download_url="https://github.com/stablyai/orca/releases",
    cask=None,
    defer_while_running=True,
    documents=(SOURCE,),
)

__all__ = [
    "BUNDLE_ID",
    "DATABASE",
    "EXCLUDED",
    "LOCAL",
    "NOISE",
    "ORCA",
    "PROFILE_DIRECTORY",
    "SOURCE",
    "SYNCED",
    "VIEW",
]
