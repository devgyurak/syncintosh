# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `text` merge path: a line-level `MergeDriver` over `merge3`.

A text file has no key structure, so it cannot go through the `ContentAdapter` route
(`parse -> merge_tree -> render`): a single changed line would have to collide with every other
line, and a positional merge is not a key merge. Instead `services` route any format with no
structural adapter here, and the line-level driver tries to reconcile. When it cannot, it reports
`conflicted`, and the content merge turns that into a **whole-file** conflict
(``AGENTS.md`` §2.9, ``core/content_merge.py``).

This module is the home of that path so a format without a structural adapter has a named
implementation to inject, instead of `services` reaching for `merge3` directly
(``AGENTS.md`` §2.2).
"""

from __future__ import annotations

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.core.models import TextMerge
from syncintosh.core.ports import MergeDriver


class TextAdapter:
    """A `MergeDriver` for `text` and every format without a structural adapter.

    It delegates to `merge3` (``TextMergeDriver``); an unreconcilable both-sides change comes
    back `conflicted`, which the content merge reports as a whole-file conflict.
    """

    def __init__(self, driver: MergeDriver | None = None) -> None:
        self._driver: MergeDriver = driver if driver is not None else TextMergeDriver()

    def merge(self, base: str, local: str, remote: str) -> TextMerge:
        """Three-way merge three texts at the line level."""
        return self._driver.merge(base, local, remote)


__all__ = ["TextAdapter"]
