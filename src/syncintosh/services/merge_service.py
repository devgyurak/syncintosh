# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: merge three parsed, flat documents at the key level.

This is a thin orchestration over the pure engine so the CLI never calls `core` directly and
`core` stays free of use-case concerns.
"""

from __future__ import annotations

from collections.abc import Mapping

from syncintosh.core.merge import merge_keys
from syncintosh.core.models import MergeResult


def merge_documents(
    base: Mapping[str, str],
    local: Mapping[str, str],
    remote: Mapping[str, str],
) -> MergeResult:
    """Merge base/local/remote key maps; conflicts are returned, not raised."""
    return merge_keys(base, local, remote)
