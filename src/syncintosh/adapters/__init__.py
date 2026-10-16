# SPDX-License-Identifier: AGPL-3.0-or-later
"""Format adapters: parse a file into the key-path tree and render it back.

An adapter preserves comments, key order, and formatting on round-trip (``AGENTS.md`` §2.5) and
implements the `ContentAdapter` port (`core/ports.py`): `parse(text) -> Tree` and
`render(tree, template) -> str`. The key-level merge rules live in `core/merge.py`, not here; a
format with no structural adapter is merged line-level through the `MergeDriver` port
(``AGENTS.md`` §2.9). Adapters are infrastructure: they may import `core`, never `cli`.
"""

from __future__ import annotations
