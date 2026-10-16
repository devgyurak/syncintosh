# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `MergeDriver` port implemented over `merge3` for text.

A line-level three-way merge of the decoded text, with git-style conflict markers. The
`conflicted` verdict comes from the merge itself (a `conflict` group from `merge3`), never from
scanning the output for markers: a settings file that legitimately contains a marker line must
not read as conflicted (``docs/design/01-sync-model.md``).

`core` owns the byte-level decision — a one-sided change never reaches this driver, and an
undecodable both-sides change is a conflict before any decode — so this module stays a pure
text merge (``AGENTS.md`` §2.9, ``core/content_merge.py``).
"""

from __future__ import annotations

from collections.abc import Sequence

from merge3 import Merge3

from syncintosh.core.models import TextMerge

LOCAL_LABEL = "local"
REMOTE_LABEL = "remote"


def _lines(text: str) -> list[str]:
    """Split keeping line endings, so a merge never adds or drops a trailing newline."""
    return text.splitlines(keepends=True)


def _terminated(chunk: str) -> str:
    """Ensure a marker block ends on its own line without inventing an empty one."""
    if chunk and not chunk.endswith("\n"):
        return chunk + "\n"
    return chunk


def _conflict_block(local_lines: Sequence[str], remote_lines: Sequence[str]) -> str:
    """A git-style conflict block; the local side is listed first."""
    return (
        f"<<<<<<< {LOCAL_LABEL}\n"
        + _terminated("".join(local_lines))
        + "=======\n"
        + _terminated("".join(remote_lines))
        + f">>>>>>> {REMOTE_LABEL}\n"
    )


class TextMergeDriver:
    """A `MergeDriver` over the line-level `merge3` algorithm."""

    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        """Three-way merge three texts, returning the merged text and the conflict verdict.

        The no-op cases short-circuit before `merge3`, which both avoids a needless O(n^2) walk
        and keeps the idempotence and one-sided-wins properties trivially true.
        """
        if local == remote:
            return TextMerge(text=local, conflicted=False)
        if local == base:
            return TextMerge(text=remote, conflicted=False)
        if remote == base:
            return TextMerge(text=local, conflicted=False)

        merger = Merge3(_lines(base), _lines(local), _lines(remote))
        parts: list[str] = []
        conflicted = False
        for group in merger.merge_groups():
            if group[0] == "conflict":
                conflicted = True
                parts.append(_conflict_block(group[2], group[3]))
            else:
                parts.append("".join(group[1]))
        return TextMerge(text="".join(parts), conflicted=conflicted)


__all__ = ["TextMergeDriver"]
