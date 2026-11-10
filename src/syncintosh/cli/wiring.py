# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring: the real implementations behind the ports, assembled for this machine.

The one place the CLI names a concrete adapter, cipher, or system class. Commands ask for `machine_ports`
and hand the result to a service, so `sync`, `resolve` and `watch` cannot drift on which catalog, scanner,
or adapter map they wire (``AGENTS.md`` §7). Every heavy import stays inside a function, so importing this
module does not pay for `cryptography`, `lxml`, or `openai` (``AGENTS.md`` §4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from syncintosh.services import SyncPorts

if TYPE_CHECKING:  # these names appear only in annotations
    from collections.abc import Mapping

    from syncintosh.core.config import SyncConfig
    from syncintosh.core.manifest import FileFormat
    from syncintosh.core.ports import (
        AgentProjectIndex,
        AppConfigDir,
        ContentAdapter,
        DenyList,
        DocumentSkips,
        LiveFiles,
        PresetCatalog,
        ProfileIndex,
        SyntaxValidator,
    )
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


@dataclass(frozen=True)
class MachineReads:
    """The ports that only read this machine: what `diff` and `doctor` need, and what `machine_ports` builds on.

    Kept apart from `SyncPorts` because assembling them never touches the key store, the scan allowlist, the lock, or
    the clone, so a read-only command can have them on a machine that has not been set up.
    """

    catalog: PresetCatalog
    deny: DenyList
    live: LiveFiles
    adapters: Mapping[FileFormat, ContentAdapter]
    validators: Mapping[ValidatorName, SyntaxValidator]
    profiles: ProfileIndex
    config_dirs: AppConfigDir
    agent_projects: AgentProjectIndex
    documents: DocumentSkips | None

    @classmethod
    def create(
        cls,
        *,
        catalog: PresetCatalog,
        deny: DenyList,
        live: LiveFiles,
        adapters: Mapping[FileFormat, ContentAdapter],
        validators: Mapping[ValidatorName, SyntaxValidator],
        profiles: ProfileIndex,
        config_dirs: AppConfigDir,
        agent_projects: AgentProjectIndex,
        documents: DocumentSkips | None,
    ) -> MachineReads:
        """Hold the read-side ports; they are assembled once, by `machine_reads`."""
        return cls(
            catalog=catalog,
            deny=deny,
            live=live,
            adapters=adapters,
            validators=validators,
            profiles=profiles,
            config_dirs=config_dirs,
            agent_projects=agent_projects,
            documents=documents,
        )


def machine_reads(paths: MachinePaths) -> MachineReads:
    """The read-side ports for this machine. The deny list, catalog and agent project index are built once here."""
    from syncintosh.presets import BuiltinCatalog, vscode
    from syncintosh.system.agent_project_index import SystemAgentProjectIndex
    from syncintosh.system.app_config_dir import ProductInfoConfigDir
    from syncintosh.system.deny_list import SystemDenyList
    from syncintosh.system.document_files import open_document_skips, open_live_files
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.profile_index import SystemProfileIndex

    home = str(paths.home)
    deny = SystemDenyList()
    catalog = BuiltinCatalog.default()
    return MachineReads.create(
        catalog=catalog,
        deny=deny,
        # A settings document kept in a database row is shown to the sync as a virtual file (`core/documents.py`).
        live=open_live_files(SystemLiveFiles(), home, catalog.documents()),
        adapters=adapters(),
        validators=validators(),
        # Reads the app's own profile list in memory, one read per cycle; it never writes or seals that file.
        profiles=SystemProfileIndex(home, deny, vscode.read_profiles),
        # The release folder the installed app says its settings live in (product-info.json), read once per cycle.
        config_dirs=ProductInfoConfigDir(home=paths.home),
        # Where each project mapped in `agent_projects` keeps the agent's data, one read per cycle.
        agent_projects=SystemAgentProjectIndex(home, deny),
        # A document the app manages itself (iTerm2 loading its prefs from a custom folder) is left alone this cycle.
        documents=open_document_skips(home, catalog.documents()),
    )


def machine_ports(config: SyncConfig, paths: MachinePaths) -> SyncPorts:
    """Every port one `sync` cycle needs, from this machine's paths and config.

    Shared by `sync`, `resolve` and `watch`. The deny list, the catalog and the agent project index are built
    once and handed to everything that needs them, so no two ports can hold different copies.
    """
    from syncintosh.adapters.text_merge import TextMergeDriver
    from syncintosh.crypto.cipher import EnvelopeCipher
    from syncintosh.crypto.file_ids import SecureFileIdSource
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.presets.orca import BUNDLE_ID as ORCA_BUNDLE_ID
    from syncintosh.system.agent_sessions import CombinedSessionActivity, CombinedSessionStore
    from syncintosh.system.app_detector import MdfindAppDetector
    from syncintosh.system.claude_sessions import ClaudeSessionActivity, ClaudeSessionStore
    from syncintosh.system.codex_sessions import CodexSessionActivity, CodexSessionStore
    from syncintosh.system.ledger import JsonLedgerStore
    from syncintosh.system.lock import FileLock
    from syncintosh.system.orca_daemon import OrcaDaemonRunningApps
    from syncintosh.system.pending import DirectoryPendingStore
    from syncintosh.system.repo_files import FileRepo
    from syncintosh.system.rotation_marker import FileRotationMarker
    from syncintosh.system.running_apps import LsappinfoRunningApps
    from syncintosh.system.scan_allow import load_scan_allow, when_tag_for
    from syncintosh.system.secret_scan import PatternSecretScanner
    from syncintosh.vcs.git import GitCLITransport

    home = str(paths.home)
    reads = machine_reads(paths)
    store = key_store(config.remote)
    transport = GitCLITransport(paths.repo_dir)
    # One object is both the `Cipher` and the `GenerationWrap`: the fetch absorbs a distributed
    # generation through the same ring the objects are opened with.
    cipher = EnvelopeCipher(store)
    # Loaded once, before the ports: a corrupt allowlist fails here, and the `when` tagger is keyed by the same key the
    # scanner uses rather than a second read of the file.
    allow = load_scan_allow(paths.scan_allow_file)
    # Both agents' sessions in one store; the activity probe asks the agent that owns a session.
    combined_sessions = CombinedSessionStore(
        (
            ClaudeSessionStore(home, config.agent_projects, reads.agent_projects, reads.deny, reads.catalog),
            CodexSessionStore(home, config.agent_projects, reads.agent_projects, reads.deny, reads.catalog),
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
        adapters=reads.adapters,
        catalog=reads.catalog,
        validators=reads.validators,
        live=reads.live,
        ledger_store=JsonLedgerStore(paths.ledger_file),
        pending_store=DirectoryPendingStore(paths.pending_dir),
        # A rotation whose push outcome is unknown: the evidence `sync` needs to settle it (never holds a key).
        rotation_marker=FileRotationMarker(paths.rotation_marker_file),
        lock=FileLock(paths.lock_file),
        deny=reads.deny,
        apps=MdfindAppDetector(),
        # Whether a preset's app is open right now: a file of a preset that defers while running waits for it to quit.
        # Orca's terminal host outlives its window, so for Orca that process counts as the app running too.
        running=OrcaDaemonRunningApps(LsappinfoRunningApps(), home=home, bundle_id=ORCA_BUNDLE_ID),
        config_dirs=reads.config_dirs,
        agent_projects=reads.agent_projects,
        documents=reads.documents,
        # Claude Code's sessions for the projects mapped with `claude-sessions`, and its own open-session signal.
        sessions=combined_sessions,
        activity=CombinedSessionActivity(combined_sessions, (ClaudeSessionActivity(home), CodexSessionActivity(home))),
        profiles=reads.profiles,
        source=SecureFileIdSource(),
        config=config,
        home=home,
        branch=transport.branch() or BRANCH,
        when_tag=when_tag_for(allow.key),
    )
