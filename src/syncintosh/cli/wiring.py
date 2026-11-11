# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring: the real implementations behind the ports, assembled for this machine.

The one place the CLI names a concrete adapter, cipher, or system class. Commands ask for `machine_ports`
and hand the result to a service, so `sync`, `resolve` and `watch` cannot drift on which catalog, scanner,
or adapter map they wire (``AGENTS.md`` §7). Every heavy import stays inside a function, so importing this
module does not pay for `cryptography`, `lxml`, or `openai` (``AGENTS.md`` §4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from syncintosh.services import SyncPorts

if TYPE_CHECKING:  # these names appear only in annotations
    from syncintosh.core.config import SyncConfig
    from syncintosh.core.manifest import FileFormat
    from syncintosh.core.ports import ContentAdapter, SyntaxValidator
    from syncintosh.core.presets import ValidatorName
    from syncintosh.system.paths import MachinePaths

# The one branch syncintosh uses. One branch keeps the merge-base model simple.
BRANCH = "main"


def key_store(remote: str):
    """The per-repo key ring, namespaced so two repos on one Mac do not share keys."""
    from syncintosh.crypto.keyring_store import KeyringKeyStore

    return KeyringKeyStore(f"syncintosh:{remote}")


def adapters() -> dict[FileFormat, ContentAdapter]:
    """Every structural adapter, by the format a preset file names.

    Wired for `sync`, `watch` and `diff` alike so the three cannot disagree on which formats merge key by key. A
    format with no entry falls back to the line-level driver. Imported here so CLI startup does not parse any
    format's machinery (``AGENTS.md`` §4); `lxml` itself loads only inside the XML adapter's first parse.
    """
    from syncintosh.adapters.jsonc import JsoncAdapter
    from syncintosh.adapters.plist import PlistAdapter
    from syncintosh.adapters.xml import XmlAdapter

    return {"jsonc": JsoncAdapter(), "xml": XmlAdapter(), "plist": PlistAdapter()}


def validators() -> dict[ValidatorName, SyntaxValidator]:
    """Every syntax check a preset may name, wired to its implementation.

    A preset names a check by the string in `ValidatorName`; a name with no implementation is
    **refused** at the write, so a missing entry here stops the file from ever being applied on this
    machine. `tests/unit/test_cli.py` compares this map against the literal, so adding a name without
    wiring it fails in CI rather than on a user's machine.
    """
    from syncintosh.system.ghostty_check import GhosttySyntaxValidator
    from syncintosh.system.json_check import JsonSyntaxValidator
    from syncintosh.system.zsh_check import ZshSyntaxValidator

    return {"json": JsonSyntaxValidator(), "zsh": ZshSyntaxValidator(), "ghostty": GhosttySyntaxValidator()}


def machine_ports(config: SyncConfig, paths: MachinePaths) -> SyncPorts:
    """Every port one `sync` cycle needs, from this machine's paths and config.

    Shared by `sync`, `resolve` and `watch`. The deny list, the catalog and the agent project index are built
    once and handed to everything that needs them, so no two ports can hold different copies.
    """
    from syncintosh.adapters.text_merge import TextMergeDriver
    from syncintosh.crypto.cipher import EnvelopeCipher
    from syncintosh.crypto.file_ids import SecureFileIdSource
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.presets import BuiltinCatalog, vscode
    from syncintosh.presets.orca import BUNDLE_ID as ORCA_BUNDLE_ID
    from syncintosh.system.agent_project_index import SystemAgentProjectIndex
    from syncintosh.system.agent_sessions import CombinedSessionActivity, CombinedSessionStore
    from syncintosh.system.app_config_dir import ProductInfoConfigDir
    from syncintosh.system.app_detector import MdfindAppDetector
    from syncintosh.system.claude_sessions import ClaudeSessionActivity, ClaudeSessionStore
    from syncintosh.system.codex_sessions import CodexSessionActivity, CodexSessionStore
    from syncintosh.system.deny_list import SystemDenyList
    from syncintosh.system.document_files import open_document_skips, open_live_files
    from syncintosh.system.ledger import JsonLedgerStore
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.lock import FileLock
    from syncintosh.system.orca_daemon import OrcaDaemonRunningApps
    from syncintosh.system.pending import DirectoryPendingStore
    from syncintosh.system.profile_index import SystemProfileIndex
    from syncintosh.system.repo_files import FileRepo
    from syncintosh.system.rotation_marker import FileRotationMarker
    from syncintosh.system.running_apps import LsappinfoRunningApps
    from syncintosh.system.scan_allow import load_scan_allow, when_tag_for
    from syncintosh.system.secret_scan import PatternSecretScanner
    from syncintosh.vcs.git import GitCLITransport

    home = str(paths.home)
    store = key_store(config.remote)
    transport = GitCLITransport(paths.repo_dir)
    deny = SystemDenyList()
    catalog = BuiltinCatalog.default()
    agent_index = SystemAgentProjectIndex(home, deny)
    # One object is both the `Cipher` and the `GenerationWrap`: the fetch absorbs a distributed
    # generation through the same ring the objects are opened with.
    cipher = EnvelopeCipher(store)
    # Loaded once, before the ports: a corrupt allowlist fails here, and the `when` tagger is keyed by the same key the
    # scanner uses rather than a second read of the file.
    allow = load_scan_allow(paths.scan_allow_file)
    # Both agents' sessions in one store; the activity probe asks the agent that owns a session.
    combined_sessions = CombinedSessionStore(
        (
            ClaudeSessionStore(home, config.agent_projects, agent_index, deny, catalog),
            CodexSessionStore(home, config.agent_projects, agent_index, deny, catalog),
        )
    )
    return SyncPorts.create(
        transport=transport,
        files=FileRepo(paths.repo_dir),
        cipher=cipher,
        wrapper=cipher,
        # The local allowlist lets a user pass a line the heuristic cannot classify (#25); it lives
        # outside the repo, so it never leaves this machine.
        scanner=PatternSecretScanner(allow),
        store=store,
        codec=KeyMaterial(),
        driver=TextMergeDriver(),
        adapters=adapters(),
        catalog=catalog,
        validators=validators(),
        # A settings document kept in a database row is shown to the sync as a virtual file (`core/documents.py`).
        live=open_live_files(SystemLiveFiles(), home, catalog.documents()),
        ledger_store=JsonLedgerStore(paths.ledger_file),
        pending_store=DirectoryPendingStore(paths.pending_dir),
        # A rotation whose push outcome is unknown: the evidence `sync` needs to settle it (never holds a key).
        rotation_marker=FileRotationMarker(paths.rotation_marker_file),
        lock=FileLock(paths.lock_file),
        deny=deny,
        apps=MdfindAppDetector(),
        # Whether a preset's app is open right now: a file of a preset that defers while running waits for it to quit.
        # Orca's terminal host outlives its window, so for Orca that process counts as the app running too.
        running=OrcaDaemonRunningApps(LsappinfoRunningApps(), home=home, bundle_id=ORCA_BUNDLE_ID),
        # The release folder the installed app says its settings live in (product-info.json), read once per cycle.
        config_dirs=ProductInfoConfigDir(home=paths.home),
        # Where each project mapped in `agent_projects` keeps the agent's data, one read per cycle.
        agent_projects=agent_index,
        # A document the app manages itself (iTerm2 loading its prefs from a custom folder) is left alone this cycle.
        documents=open_document_skips(home, catalog.documents()),
        # Claude Code's sessions for the projects mapped with `claude-sessions`, and its own open-session signal.
        sessions=combined_sessions,
        activity=CombinedSessionActivity(combined_sessions, (ClaudeSessionActivity(home), CodexSessionActivity(home))),
        # Reads the app's own profile list in memory, one read per cycle; it never writes or seals that file.
        profiles=SystemProfileIndex(home, deny, vscode.read_profiles),
        source=SecureFileIdSource(),
        config=config,
        home=home,
        branch=transport.branch() or BRANCH,
        when_tag=when_tag_for(allow.key),
    )
