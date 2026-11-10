# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dangerous paths refused before collection.

These are refused **before anything is read**, in code, not by asking the user
(``AGENTS.md`` §3.4): a private key or a cloud credential must never enter a repo, however the
user configured their presets.

The judgement is by **real path**. A symlink is the obvious bypass — a preset naming
``~/notes/link`` whose target is ``~/.ssh`` would look innocent at the path level — so both the
resolved target *and* the unresolved path under `$HOME` are checked. Resolving only the target
would miss a denied directory that is itself a symlink out of the tree; checking only the
unresolved path would miss a link pointing in.

The comparison is **folded** (NFC + casefold), the same way path identity is folded everywhere
else (``core.paths``). macOS's default APFS is case-insensitive, so ``~/.SSH/id_ed25519`` names
the very same file as ``~/.ssh/id_ed25519`` — and `os.path.realpath` does not normalize the case
it is given, so a case-sensitive comparison here is a bypass, not a nicety.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from syncintosh.core.paths import collision_key
from syncintosh.system.paths import MachinePaths

# Orca's whole data folder is denied. Beside the settings row a preset syncs it holds credentials and state in places
# that change with every release: the secret-protection file, an E2EE key pair, a session-authority key, the terminal
# host's token (`daemon/`), browser session stores under `Partitions/` and `Network/`, `codex-runtime-home/` auth files,
# `terminal-history/`, `orchestration.db`, the profile database with its `-wal`/`-shm`, and `orca-data.json` with its
# backups and its `.sqlite-export.<revision>.json` copies, which hold the whole state, credentials included. Naming
# them one by one fails open for the next folder Orca adds, so the folder is denied as a tree (``AGENTS.md`` §3.4). The
# one exception is the virtual `@profile-state/settings.json` of the `local-default` profile (`_is_orca_view`): it is
# not a file, and it is how the preset reaches the settings row.
_ORCA_ROOT = "Library/Application Support/orca"

# Relative to `$HOME`. A directory entry denies everything beneath it. An element may be `*` (any one element) or a
# name pattern; a wildcard never spans a `/`, so there is no recursion and `JetBrains/*/ssl` is the `ssl` of one
# release folder, not of every descendant.
DENIED_PATHS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".kube/config",
    "Library/Cookies",
    "Library/Caches",
    ".cache",
    # syncintosh's own machine-local state: the apply ledger, the pending plaintext, and the
    # secret scan's allowlist (which holds an HMAC key). It is never collected by a built-in
    # preset, but a user-added item could name it; refusing it by construction keeps this
    # machine's state — and the allowlist key — off the other machines (``AGENTS.md`` §3.3).
    ".local/share/syncintosh",
    # Docker context TLS material: a client certificate and its private key, next to the
    # context that uses them. `AGENTS.md` §3.4 names it, and the Docker preset never tracks
    # this path — the entry exists so a user-added item cannot pull it in either.
    #
    # Deliberately not listed yet: `.docker/certs.d` and `.docker/tls` (Docker's registry-CA
    # locations) and the `ca.pem`/`cert.pem`/`key.pem` defaults the CLI reference documents. None
    # existed on the inspected machine, so no preset or item can reach them today; the private-key
    # secret scan keeps a key out of the repo if a user adds one by hand. A follow-up adds them
    # when a real setup needs it (docs/design/presets.md, the `docker` notes).
    ".docker/contexts/tls",
    # Zed's Dev build keeps its sign-in token here as plaintext (`zed_credentials_provider.rs`). The `zed`
    # preset never tracks it; the entry keeps a user-added item or a glob from pulling it in.
    ".config/zed/development_credentials",
    # JetBrains IDEs keep database credentials and connection material in a release folder next to the settings a
    # preset syncs (`DataGrip2026.2/…`). `*` is that folder, whichever release it is, and the logical `@DataGrip`
    # element of a record path matches it too. `dataSources.xml` holds connection URLs and user names, `security.xml`
    # the trust store, `c.kdbx`/`c.pwd` the credential store, `ssl` and the key files TLS and SSH key material.
    # Blocked here, in code, whatever the catalog says (``AGENTS.md`` §3.4).
    "Library/Application Support/JetBrains/*/ssl",
    "Library/Application Support/JetBrains/*/jdbc-drivers",
    "Library/Application Support/JetBrains/*/consoles",
    "Library/Application Support/JetBrains/*/dataSourcesHistory",
    "Library/Application Support/JetBrains/*/workspace",
    "Library/Application Support/JetBrains/*/tasks",
    "Library/Application Support/JetBrains/*/settingsSync",
    "Library/Application Support/JetBrains/*/options/dataSources",
    "Library/Application Support/JetBrains/*/*.key",
    "Library/Application Support/JetBrains/*/c.kdbx",
    "Library/Application Support/JetBrains/*/c.pwd",
    # Product-specific and plugin files in `options/` that carry a host, a user name, a token or a trust decision:
    # SSH and WSL connections, deployment servers, container registries, Kubernetes and HTTP-client settings, the
    # proxy and certificate stores, and the GitHub/GitLab accounts. None is in a preset's file list; they are named
    # here so a user-added item or a glob cannot pull one in. `*` inside a name covers `dataSources.local.xml` and the
    # numbered `sshRecentConnections*.xml` variants.
    "Library/Application Support/JetBrains/*/options/dataSources*.xml",
    "Library/Application Support/JetBrains/*/options/security.xml",
    "Library/Application Support/JetBrains/*/options/remote-servers.xml",
    "Library/Application Support/JetBrains/*/options/sshConfigs.xml",
    "Library/Application Support/JetBrains/*/options/webServers.xml",
    "Library/Application Support/JetBrains/*/options/webDeployTransportHostTargets.xml",
    "Library/Application Support/JetBrains/*/options/databaseDrivers.xml",
    "Library/Application Support/JetBrains/*/options/certificates.xml",
    "Library/Application Support/JetBrains/*/options/proxy.settings.xml",
    "Library/Application Support/JetBrains/*/options/github.xml",
    "Library/Application Support/JetBrains/*/options/gitlab.xml",
    "Library/Application Support/JetBrains/*/options/docker.xml",
    "Library/Application Support/JetBrains/*/options/docker-registry.xml",
    "Library/Application Support/JetBrains/*/options/kubernetes-settings.xml",
    "Library/Application Support/JetBrains/*/options/http-client.xml",
    "Library/Application Support/JetBrains/*/options/sshRecentConnections*.xml",
    "Library/Application Support/JetBrains/*/options/wslRecentConnections*.xml",
    "Library/Application Support/JetBrains/*/options/recentConnections.xml",
    "Library/Application Support/JetBrains/*/options/trusted-paths.xml",
    # Basis for the group above: each name is a storage name seen in intellij-community's
    # `platform/platform-impl/resources/settings/ide-settings-model.json`. That the file holds a host, a user name, a
    # token or a trust decision is inferred from the name, not verified; it is blocked conservatively.
    # The same files under `options/mac/` (`RoamingType.PER_OS` storages live there).
    "Library/Application Support/JetBrains/*/options/mac/dataSources*.xml",
    "Library/Application Support/JetBrains/*/options/mac/security.xml",
    "Library/Application Support/JetBrains/*/options/mac/remote-servers.xml",
    "Library/Application Support/JetBrains/*/options/mac/sshConfigs.xml",
    "Library/Application Support/JetBrains/*/options/mac/webServers.xml",
    "Library/Application Support/JetBrains/*/options/mac/webDeployTransportHostTargets.xml",
    "Library/Application Support/JetBrains/*/options/mac/databaseDrivers.xml",
    "Library/Application Support/JetBrains/*/options/mac/certificates.xml",
    "Library/Application Support/JetBrains/*/options/mac/proxy.settings.xml",
    "Library/Application Support/JetBrains/*/options/mac/github.xml",
    "Library/Application Support/JetBrains/*/options/mac/gitlab.xml",
    "Library/Application Support/JetBrains/*/options/mac/docker.xml",
    "Library/Application Support/JetBrains/*/options/mac/docker-registry.xml",
    "Library/Application Support/JetBrains/*/options/mac/kubernetes-settings.xml",
    "Library/Application Support/JetBrains/*/options/mac/http-client.xml",
    "Library/Application Support/JetBrains/*/options/mac/sshRecentConnections*.xml",
    "Library/Application Support/JetBrains/*/options/mac/wslRecentConnections*.xml",
    "Library/Application Support/JetBrains/*/options/mac/recentConnections.xml",
    "Library/Application Support/JetBrains/*/options/mac/trusted-paths.xml",
    _ORCA_ROOT,
    # A preferences domain's plist belongs to `cfprefsd`, which caches it and rewrites the file on its own schedule:
    # a read of the file misses what is still in memory and a write under it is lost or half-applied. The one
    # supported door is `defaults`, behind a virtual `Library/Preferences/@defaults/<domain>.plist` view
    # (`system/defaults_domains.py`), so the real file is refused by construction (issue #58). The `.private` plist is
    # iTerm2's second domain file, listed in its cask's zap paths; neither is ever a preset's file.
    "Library/Preferences/com.googlecode.iterm2.plist",
    "Library/Preferences/com.googlecode.iterm2.private.plist",
    # Coding agents' own state (issue #56). Only a project's `memory/` and, later, its transcripts are ever synced, and
    # those are reached through a logical `@<name>` path; everything else the CLIs keep beside them is account metadata,
    # environment dumps, file backups or live process state. A wildcard covers the names the CLIs add and rotate
    # (`.claude.json.backup`, `daemon.sock`, `state_5.sqlite-wal`), so the next one fails closed.
    ".claude.json*",
    # The plaintext OAuth file Claude Code falls back to when it cannot use the keychain.
    ".claude/.credentials.json",
    ".claude/session-env",
    ".claude/shell-snapshots",
    ".claude/file-history",
    ".claude/paste-cache",
    ".claude/history.jsonl",
    ".claude/cache",
    ".claude/daemon*",
    # One `<pid>.json` per open session: a live-process signal, read by the activity probe and never collected.
    ".claude/sessions",
    ".codex/auth.json",
    ".codex/installation_id",
    ".codex/history.jsonl",
    ".codex/shell_snapshots",
    ".codex/sqlite",
    ".codex/logs_*.sqlite*",
    # Codex's thread database and its write-ahead files: syncintosh never writes it (decision D13), and it holds the
    # absolute rollout paths and account identifiers.
    ".codex/*.sqlite*",
    ".codex/thread-writer-locks",
    ".codex/app-server-*",
    ".codex/ipc",
)


def _under(candidate: str, root: str) -> bool:
    """True when `candidate` is `root` or lies beneath it, folded for case and normalization."""
    folded_candidate = collision_key(candidate)
    folded_root = collision_key(root)
    return folded_candidate == folded_root or folded_candidate.startswith(folded_root + "/")


def _element_pattern(element: str) -> re.Pattern[str]:
    """The regex one path element of a denied entry means: `*` any run of characters, everything else literal."""
    return re.compile("".join("[^/]*" if char == "*" else re.escape(char) for char in collision_key(element)))


def _denied_by(candidate: str, base: str, pattern: tuple[str, ...]) -> bool:
    """True when `candidate` is `base` plus elements matching `pattern` (or lies beneath such a path), folded.

    `base` is the literal part of the entry, resolved or as written; `pattern` is the rest, where an element
    may hold `*`. One element is matched by one pattern element, so the match never crosses a `/`.
    """
    folded, folded_base = collision_key(candidate), collision_key(base)
    if folded != folded_base and not folded.startswith(folded_base + "/"):
        return False
    below = folded[len(folded_base) + 1 :].split("/") if len(folded) > len(folded_base) else []
    return len(below) >= len(pattern) and all(
        _element_pattern(want).fullmatch(have) is not None for want, have in zip(pattern, below, strict=False)
    )


def _denied_entry(denied: str, home: Path, *, real_candidate: str, written_candidate: str) -> bool:
    """True when either spelling of the candidate falls under one `DENIED_PATHS` entry."""
    elements = denied.split("/")
    literal = next((index for index, element in enumerate(elements) if "*" in element), len(elements))
    base = home.joinpath(*elements[:literal])
    pattern = tuple(elements[literal:])
    return _denied_by(real_candidate, os.path.realpath(base), pattern) or _denied_by(
        written_candidate, str(base), pattern
    )


def _is_orca_view(candidate: str, home: Path) -> bool:
    """True when `candidate` is exactly the virtual settings view of Orca's `local-default` profile, folded."""
    base = collision_key(str(home.joinpath(_ORCA_ROOT, "profiles")))
    folded = collision_key(candidate)
    if not folded.startswith(base + "/"):
        return False
    return folded[len(base) + 1 :].split("/") == ["local-default", "@profile-state", "settings.json"]


def is_denied(path: Path, home: Path) -> bool:
    """True when `path` is a dangerous path that must never be collected.

    `path` is the live path to read; `home` is the user's home directory. Both the real path and
    the path as written are compared, so neither a symlink into a denied tree nor a denied tree
    replaced by a symlink gets through. Orca's virtual settings view is exempt from the Orca tree
    entry only when **both** spellings are that view, so a symlink dressed as the view still
    resolves into the denied tree.
    """
    real_home: Path = Path(os.path.realpath(home))
    real_candidate = os.path.realpath(path)
    # `abspath`, not `realpath`: this is the path as written, so a denied directory that is
    # itself a symlink is still recognised by its name.
    written_home = Path(os.path.abspath(home))  # noqa: PTH100 — resolve() would follow a link
    written_candidate = os.path.abspath(path)  # noqa: PTH100 — resolve() would follow the link we must detect
    view = _is_orca_view(real_candidate, real_home) and _is_orca_view(written_candidate, written_home)
    return any(
        _denied_entry(denied, real_home, real_candidate=real_candidate, written_candidate=written_candidate)
        for denied in DENIED_PATHS
        if not (view and denied == _ORCA_ROOT)
    )


__all__ = ["DENIED_PATHS", "SystemDenyList", "is_denied"]


class SystemDenyList:
    """The `DenyList` port over `is_denied` and a home-escape check."""

    def is_denied(self, home: str, relpath: str) -> bool:
        """True when `home/relpath` is a dangerous path that must never be collected."""
        home_path = Path(home)
        if is_denied(home_path / relpath, home_path):
            return True
        # `XDG_DATA_HOME` can move the state directory off the default `.local/share`, so the
        # literal `DENIED_PATHS` entry is not enough: whatever path the state directory resolves to
        # holds this machine's ledger, its pending plaintext, and the scan allowlist key. Deriving
        # it here is why this check lives in the adapter and not in the pure `DENIED_PATHS` list.
        state_dir = MachinePaths.default(home_path).state_dir
        return _under(os.path.realpath(home_path / relpath), os.path.realpath(state_dir))

    def escapes(self, home: str, relpath: str) -> bool:
        """True when the record's target leaves `$HOME` once symlinks are resolved.

        The **parent directory** is resolved first, so a symlinked directory cannot be used to
        write outside the home tree; the final element is `apply`'s own symlink check
        (``docs/design/03-manifest.md``).
        """
        real_home = Path(os.path.realpath(home))
        parent = Path(os.path.realpath(Path(home) / relpath)).parent
        return parent != real_home and real_home not in parent.parents
