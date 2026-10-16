# SPDX-License-Identifier: AGPL-3.0-or-later
"""Byte-level content merge for one record.

The adapter's key-level rules run on decoded text, so the text must first decode as UTF-8. A
blob that does not (a `binary` record, or a text file that picked up a stray byte) cannot go
through a line-level merge: when both sides changed it, the only honest verdict is a conflict
(``docs/design/03-manifest.md``). A one-sided change never needs the merge, so it is still
applied — that is the whole point of routing only a *both-sides* change into the merge.

This module is pure: it takes plain bytes and a `MergeDriver` port and returns a frozen
`ContentMerge`, so it is testable with no adapter and no filesystem (``AGENTS.md`` §2.1, §2.9).
"""

from __future__ import annotations

from syncintosh.core.models import ContentMerge
from syncintosh.core.ports import MergeDriver


def _decode_all(base: bytes, local: bytes, remote: bytes) -> tuple[str, str, str] | None:
    """Decode all three blobs, or `None` when any of them is not UTF-8."""
    try:
        return base.decode("utf-8"), local.decode("utf-8"), remote.decode("utf-8")
    except UnicodeDecodeError:
        return None


def merge_content(base: bytes, local: bytes, remote: bytes, driver: MergeDriver) -> ContentMerge:
    """Three-way merge one record's plaintext bytes.

    A one-sided change is taken directly, without invoking the driver; only a both-sides change
    reaches the driver, and an undecodable both-sides change is a conflict with the local side
    as the unresolved candidate.
    """
    if local == remote:
        return ContentMerge(data=local, conflicted=False)
    if local == base:
        return ContentMerge(data=remote, conflicted=False)
    if remote == base:
        return ContentMerge(data=local, conflicted=False)
    decoded = _decode_all(base, local, remote)
    if decoded is None:
        return ContentMerge(data=local, conflicted=True)
    base_text, local_text, remote_text = decoded
    result = driver.merge(base_text, local_text, remote_text)
    return ContentMerge(data=result.text.encode("utf-8"), conflicted=result.conflicted)
