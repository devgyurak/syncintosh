# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure domain layer: immutable models, the merge engine, and ports.

Nothing in here touches the filesystem, the network, the keychain, or a clock. Code that
needs the outside world declares a protocol in :mod:`syncintosh.core.ports` and lets an
infrastructure package implement it (``AGENTS.md`` §2.1).
"""

from __future__ import annotations
