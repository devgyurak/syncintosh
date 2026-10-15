# SPDX-License-Identifier: AGPL-3.0-or-later
"""syncintosh — keep developer-environment settings in step across Macs.

The repository is organized in one-way layers (see ``AGENTS.md`` §2):
``cli -> services -> core``, with infrastructure (``adapters`` · ``presets`` ·
``crypto`` · ``vcs`` · ``system`` · ``llm``) implementing the ports declared in
``syncintosh.core.ports``.
"""

from __future__ import annotations

__all__: list[str] = []
