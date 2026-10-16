# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local-machine infrastructure.

Implements the ports declared in ``syncintosh.core.ports`` that touch the machine itself —
file storage for the repo working tree here, with app detection, launchd, and the watchdog
landing here later. Never imports ``cli`` (``AGENTS.md`` §2).
"""

from __future__ import annotations
