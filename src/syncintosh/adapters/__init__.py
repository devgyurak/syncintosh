# SPDX-License-Identifier: AGPL-3.0-or-later
"""Format adapters: parse, merge, and serialize each tracked file's format.

An adapter preserves comments, key order, and formatting on round-trip and holds the key-level
merge rules for its format, delegating the line-level merge to the `MergeDriver` port
(``AGENTS.md`` §2.5, §2.9). Adapters are infrastructure: they may import `core`, never `cli`.
"""

from __future__ import annotations
