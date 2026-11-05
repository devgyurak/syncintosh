# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every policy each built-in preset has shipped (issue #24): the append-only history behind `rev`.

A preset's **policy** is what decides which bytes an object holds — its files, formats, noise and
excluded keys, and profile layout (`core/policy.py`). The manifest stamps each preset with the
revision (this tuple's length) and digest the writing build used, so a build can tell a repo written
under an older policy (migrate it) from one written under a policy it has never heard of (stop).

**Append, never edit.** When a change to a preset alters its policy digest, add the new digest to the
end of that preset's tuple in the same commit, and to `tests/fixtures/policy_history_shipped.json`.

The digest sees only data. If you change what the **code** does with a preset's files (the projection, the
ignore carry, a strip you switch back on), no field changes, so raise that preset's `policy_epoch` first: it
is part of the digest, and the bump is what produces the new entry to append.

Rewriting or reordering an entry makes every repo stamped under it read as `unknown`.
`tests/unit/test_policy_history.py` fails until the last entry equals the current digest, and prints the
digest to append; it also fails if an entry the fixture pins has changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

POLICY_HISTORY: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "zsh": ("1914db8679f8fc449a7c8b0eeb43a67e8ab6c38bd43d44a21acf052bb6bbcec8",),
        "vscode": ("df4dcae1e246dcbc04cf2bdde13dbb167dacd746d8f26729a040ad497356b602",),
        "docker": ("c7551c6c38d3edcecfa3c038eba3a915b9688423d6cc77474a0628f132b55983",),
        "zed": ("42b152727414e64f19e6e819cb4af51f97b6708778cbd2575444ac3fb89636d2",),
    }
)
