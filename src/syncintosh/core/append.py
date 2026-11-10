# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merge rule for an **append-only** file: a transcript is only ever extended, so there is nothing to 3-way merge.

A coding agent's session transcript grows at the end and is not edited in place while idle (issue #56). Two copies of
one session are therefore related in one of three ways, and the rule needs no base:

* **same** — the bytes are equal: keep it.
* **extended** — one side is a byte prefix of the other: the longer one is the same session, further along. It wins.
* **forked** — neither is a prefix of the other: the session was continued on both Macs. There is no safe union of
  two histories, so the caller keeps the repository's copy and does not collect the local one (decision D2).

The rule is pure and symmetric: swapping the sides swaps `longer`, never the bytes. It holds no policy about *which*
files are append-only; the preset says that (`PresetFile.append_only`).

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

# How two copies of one append-only file relate. Serialized nowhere; a `Literal`, not an `Enum` (``AGENTS.md`` §4).
AppendKind = Literal["same", "extended", "forked"]
SAME: Final = "same"
EXTENDED: Final = "extended"
FORKED: Final = "forked"

# Which side is further along when one extends the other.
Longer = Literal["local", "remote"]


@dataclass(frozen=True)
class AppendMerge:
    """The outcome of comparing a local and a remote copy of an append-only file.

    `data` is the bytes to keep: the shared bytes for `same`, the longer side for `extended`, and the **remote**
    copy for `forked` (the repository's side is kept, decision D2). `longer` is set only for `extended`.
    """

    kind: AppendKind
    data: bytes = field(repr=False)
    longer: Longer | None = None

    @property
    def forked(self) -> bool:
        """True when neither side is a prefix of the other."""
        return self.kind == FORKED


def append_merge(local: bytes, remote: bytes) -> AppendMerge:
    """Compare two copies of an append-only file (see the module docstring)."""
    if local == remote:
        return AppendMerge(kind=SAME, data=remote)
    if remote.startswith(local):
        return AppendMerge(kind=EXTENDED, data=remote, longer="remote")
    if local.startswith(remote):
        return AppendMerge(kind=EXTENDED, data=local, longer="local")
    return AppendMerge(kind=FORKED, data=remote)


__all__ = ["EXTENDED", "FORKED", "SAME", "AppendKind", "AppendMerge", "Longer", "append_merge"]
