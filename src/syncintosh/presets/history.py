# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every policy each built-in preset has shipped (issue #24): the append-only history behind `rev`.

A preset's **policy** is what decides which bytes an object holds — its files, formats, noise and
excluded keys, and profile layout (`core/policy.py`). The manifest stamps each preset with the
revision (this tuple's length) and digest the writing build used, so a build can tell a repo written
under an older policy (migrate it) from one written under a policy it has never heard of (stop).

**Append, never edit.** When a change to a preset alters its policy digest, add the new digest to the
end of that preset's tuple in the same commit. Rewriting or reordering an entry makes every repo
stamped under it read as `unknown`. `tests/unit/test_policy_history.py` fails until the last entry
equals the current digest, and prints the digest to append.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

POLICY_HISTORY: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "zsh": ("4f00dfa1260df3fb537e9f6c9bb939af1e8da29ae1490eeda0d7d44c7d9b9bd7",),
        "vscode": ("a43b767bf032188078795433792d6c9f39edee4d3f69a3cdf5ab3d7308d3fc08",),
        "docker": ("ea4e9891e8dbdbd6b6965319993f69e0e12f7931950a23644a1b6305ec63989c",),
    }
)
