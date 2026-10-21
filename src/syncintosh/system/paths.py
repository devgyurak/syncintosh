# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where syncintosh keeps its machine-local state.

The layout is fixed by ``docs/design/README.md`` ("Runtime & layout"): the config under
``$XDG_CONFIG_HOME``, and the clone, the apply ledger, the pending state, and the lock under
``$XDG_DATA_HOME``. The ledger and the pending state are **outside** the repo and never inside
git (``AGENTS.md`` §3.3); the plaintext mirror is gone — collected bytes stay in memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _config_root(home: Path) -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")


def _data_root(home: Path) -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")


@dataclass(frozen=True)
class MachinePaths:
    """Every machine-local path syncintosh touches, derived from `$HOME`."""

    home: Path
    config_dir: Path
    state_dir: Path

    @classmethod
    def default(cls, home: Path | None = None) -> MachinePaths:
        """The paths for `home` (defaulting to the real home), honouring `XDG_*` overrides."""
        resolved = Path.home() if home is None else home
        return cls(
            home=resolved,
            config_dir=_config_root(resolved) / "syncintosh",
            state_dir=_data_root(resolved) / "syncintosh",
        )

    @property
    def config_file(self) -> Path:
        """`config.json` — non-secret values only."""
        return self.config_dir / "config.json"

    @property
    def repo_dir(self) -> Path:
        """The clone: the git working tree holding the ciphertext."""
        return self.state_dir / "repo"

    @property
    def ledger_file(self) -> Path:
        """`applied.json` — content digests only, never plaintext."""
        return self.state_dir / "applied.json"

    @property
    def scan_allow_file(self) -> Path:
        """`scan-allow.json` — the secret scan's local allowlist and its HMAC key.

        Machine-local and **never synced** (``AGENTS.md`` §3.3): a synced allowlist would let one
        machine tell another what not to scan, so it lives beside the ledger, outside the repo, at
        mode 0600. It is deliberately not in `config.json`, which holds only non-secret values.
        """
        return self.state_dir / "scan-allow.json"

    @property
    def pending_dir(self) -> Path:
        """The unresolved merge state, plaintext, outside the repo."""
        return self.state_dir / "pending"

    @property
    def lock_file(self) -> Path:
        """The advisory lock the CLI and the daemon share."""
        return self.state_dir / "sync.lock"


__all__ = ["MachinePaths"]
