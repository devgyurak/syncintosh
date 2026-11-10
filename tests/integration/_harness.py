# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one integration harness: the real `machine_ports` wiring over a temp HOME, with the outside world faked.

Every flow test used to rebuild the thirty ports by hand, so a port added to the wiring reached the tests only when
someone remembered to copy it. This module calls `machine_ports` for the machine's own paths and replaces only
what a test must not touch: the keychain, the app, process and session probes (`mdfind`, `lsappinfo`, `ps`, the
agents' CLIs, none of which a Linux CI runner has), the lock timeout, and the scrypt cost. A test that needs a narrower
or fake port passes it by name (``AGENTS.md`` §5.6).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from syncintosh.cli.wiring import machine_ports
from syncintosh.core.config import SyncConfig
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.services.sync import (
    NO_APP_INFO,
    NO_RUNNING_INFO,
    NO_SESSION_ACTIVITY,
    NO_SESSION_STORE,
    SyncPorts,
    SyncReport,
    sync,
)
from syncintosh.system.config import load_config
from syncintosh.system.lock import FileLock
from syncintosh.system.paths import MachinePaths

# Cheap scrypt parameters: the real cost (~128 MiB) would make every ring import take seconds (``AGENTS.md`` §3.1).
CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})


def cheap_codec() -> KeyMaterial:
    """A key codec that wraps and unwraps rings with `CHEAP` parameters only."""
    return KeyMaterial(params=CHEAP, allowed=ALLOWED)


def machine_paths(home: Path) -> MachinePaths:
    """The paths of the machine rooted at `home`; `XDG_*` is ignored so two machines in a test keep apart."""
    return MachinePaths(
        home=home, config_dir=home / ".config" / "syncintosh", state_dir=home / ".local" / "share" / "syncintosh"
    )


def sync_ports(
    home: Path, store: Any, *, codec: KeyMaterial | None = None, config: SyncConfig | None = None, **ports: Any
) -> SyncPorts:
    """`machine_ports` for the machine at `home`, with `store` as its key ring and `ports` replacing the named ports.

    The cipher and the generation wrap are rebuilt from `store`: they hold the ring they were built with, so replacing
    `store` alone would leave them on the real keychain.
    """
    paths = machine_paths(home)
    wired = machine_ports(load_config(paths.config_file) if config is None else config, paths)
    cipher = EnvelopeCipher(store)
    return replace(
        wired,
        **{
            "store": store,
            "cipher": cipher,
            "wrapper": cipher,
            "codec": cheap_codec() if codec is None else codec,
            "lock": FileLock(paths.lock_file, timeout_seconds=5),
            "apps": NO_APP_INFO,
            "running": NO_RUNNING_INFO,
            # Spotlight is the default locator; nothing may ask it from a test.
            "config_dirs": None,
            # The real session stores run `claude --version`, `codex --version` and `ps`, and the real agent project
            # index reads `CLAUDE_CONFIG_DIR` from the process environment: a test that wants either passes its own.
            "sessions": NO_SESSION_STORE,
            "activity": NO_SESSION_ACTIVITY,
            "agent_projects": None,
            **ports,
        },
    )


def run_sync(
    home: Path,
    store: Any,
    *,
    codec: KeyMaterial | None = None,
    config: SyncConfig | None = None,
    adopt: Any = None,
    resolving: bool = False,
    decide: Any = None,
    **ports: Any,
) -> SyncReport:
    """One sync cycle for the machine at `home` (see `sync_ports` for what `ports` replaces)."""
    return sync(
        sync_ports(home, store, codec=codec, config=config, **ports), adopt=adopt, resolving=resolving, decide=decide
    )
