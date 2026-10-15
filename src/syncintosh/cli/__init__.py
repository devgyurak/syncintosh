# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command-line layer.

Commands parse arguments, call a service, and render with Rich/Typer. Judgment lives in
`core`/`services`, never here (``AGENTS.md`` §2.3).
"""

from __future__ import annotations
