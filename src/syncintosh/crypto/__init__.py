# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cryptographic envelope, key ring, and key wrapping (infrastructure).

Implements the ports declared in ``syncintosh.core.ports``. ``cryptography`` is imported only
inside this layer, never by ``core``/``services``/``cli`` (``AGENTS.md`` §2, §4).
"""

from __future__ import annotations
