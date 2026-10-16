# SPDX-License-Identifier: AGPL-3.0-or-later
"""Version-control transports.

`vcs` implements the `GitTransport` port with the real `git` CLI. Infrastructure: it may import
`core`, never `cli` (``AGENTS.md`` §2).
"""

from __future__ import annotations
