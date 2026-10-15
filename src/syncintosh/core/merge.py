# SPDX-License-Identifier: AGPL-3.0-or-later
"""Key-level three-way merge.

A line-level git merge can break JSON/XML syntax, so the unit of comparison is a **key**.
The rules are:

* a key changed on only one side always wins (idempotent when nothing changed);
* a key changed on **both** sides to **different** values is the only conflict;
* both sides deleting a key, or both adding the same value, is not a conflict.

When a both-sides change conflicts, the local value is kept in ``merged`` and the conflict
is recorded so the resolver can override it. This module is pure: it takes and returns
plain values so it can be tested and property-checked with no setup.
"""

from __future__ import annotations

from collections.abc import Mapping

from syncintosh.core.models import Conflict, MergeResult


def merge_keys(
    base: Mapping[str, str],
    local: Mapping[str, str],
    remote: Mapping[str, str],
) -> MergeResult:
    """Merge three ``key -> value`` mappings.

    A key value of ``None`` (a key absent on that side) is treated as a deletion. When the
    union of keys has a both-sides change, ``local`` wins in ``merged`` and the key is added
    to ``conflicts``.
    """
    merged: dict[str, str] = {}
    conflicts: list[Conflict] = []

    for key in sorted({*base, *local, *remote}):
        base_value = base.get(key)
        local_value = local.get(key)
        remote_value = remote.get(key)

        if local_value == remote_value:
            chosen = local_value
        elif local_value == base_value:
            chosen = remote_value  # only the remote changed
        elif remote_value == base_value:
            chosen = local_value  # only the local changed
        else:
            chosen = local_value
            conflicts.append(Conflict(key=key, base=base_value, local=local_value, remote=remote_value))

        if chosen is not None:
            merged[key] = chosen

    return MergeResult(merged=merged, conflicts=tuple(conflicts))
