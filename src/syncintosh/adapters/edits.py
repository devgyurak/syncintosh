# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text edits shared by the adapters that render by editing the original text.

An adapter that rewrites a file in place (JSONC, XML) produces a list of :class:`TextEdit`; this module is the one
place that settles them into offset order and applies them, so two adapters cannot disagree on what an overlap means.
"""

from __future__ import annotations

from syncintosh.core.errors import RenderRefused
from syncintosh.core.ports import TextEdit

# Two edits to one file overlap (an array element or an object member), so one of them would be dropped; the text names
# only the cause.
EDIT_OVERLAP_REFUSED = "two edits to this file overlap, so it cannot be written without losing one of them"


def settle(edits: list[TextEdit]) -> list[TextEdit]:
    """The edits in offset order, without the ones an earlier edit already covers.

    An edit that starts inside an earlier one is skipped only when it takes nothing with it: it removes text the earlier
    edit already removed. One that carries text, or reaches past the earlier edit, would be lost without a trace, so it
    refuses the render instead.
    """
    settled: list[TextEdit] = []
    cursor = 0
    for edit in sorted(edits, key=lambda item: item.start):
        if edit.start < cursor:
            if edit.text or edit.end > cursor:
                raise RenderRefused(EDIT_OVERLAP_REFUSED)
            continue
        settled.append(edit)
        cursor = edit.end
    return settled


def apply_edits(text: str, edits: list[TextEdit]) -> str:
    """Apply the edits in offset order, as :func:`settle` leaves them."""
    pieces: list[str] = []
    cursor = 0
    for edit in settle(edits):
        pieces.append(text[cursor : edit.start])
        pieces.append(edit.text)
        cursor = edit.end
    pieces.append(text[cursor:])
    return "".join(pieces)
