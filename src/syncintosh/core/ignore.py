# SPDX-License-Identifier: AGPL-3.0-or-later
"""The user's own ignore list: which top-level settings this machine keeps to itself.

VS Code's ``settingsSync.ignoredSettings`` names settings that a machine does not share. VS Code
does not delete an ignored key from what it uploads; it leaves the remote's value in place
(``updateIgnoredSettings(content, remoteContent, ignored)``). syncintosh does the same, which is
why this is a *carry* and not a strip (:mod:`syncintosh.core.projection`): stripping the key would
make the 3-way merge read it as deleted and erase the value from every other machine.

This module only reads the list. It takes the **parsed** settings (``AGENTS.md`` §2.1), so it
holds no file, adapter, or I/O, and returns the top-level keys to carry plus the entries it could
not honour.

* ``settingsSync.ignoredSettings`` and its legacy name ``sync.ignoredSettings`` are both read and
  their entries pooled.
* An entry ``-x`` cancels the **user's own** ``x``. VS Code uses it to subtract from a built-in
  default set, which syncintosh does not have; a ``-x`` with no matching ``x`` is reported instead
  of silently doing nothing, and it never revives a key a preset strips.
* ``settingsSync.ignoredSettings`` and ``settingsSync.ignoredExtensions`` cannot be ignored
  (``disallowSyncIgnore`` upstream), so the list itself does travel.
* Matching is by top-level key: ``"[python]"`` is one key, and so is ``"editor.fontSize"``.
* Precedence: excluded > preset noise > the user's list. A key the preset already strips is never
  also carried, because carrying would put a value back that the preset exists to keep out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from syncintosh.core.errors import IgnoreListMalformed
from syncintosh.core.presets import PresetFile
from syncintosh.core.tree import KeyPath

IGNORED_SETTINGS_KEY: Final = "settingsSync.ignoredSettings"
LEGACY_IGNORED_SETTINGS_KEY: Final = "sync.ignoredSettings"

# The keys VS Code refuses to let a user ignore, so a machine cannot hide the list that governs it.
NON_IGNORABLE: Final = frozenset({IGNORED_SETTINGS_KEY, "settingsSync.ignoredExtensions"})

_CANCEL: Final = "-"


@dataclass(frozen=True)
class IgnoreSpec:
    """The top-level settings this machine ignores, built once per cycle by :meth:`create`.

    `keys` are the user's ignored keys after cancellation, before the preset's policy is applied;
    :meth:`paths_for` applies it. `unmatched_cancellations` are the ``-x`` entries with no ``x``
    to cancel, kept so a report can say that one did nothing (``docs/design/presets.md``).
    """

    # Setting names come from the user's file, so a repr never shows them (``AGENTS.md`` §4).
    keys: frozenset[str] = field(default=frozenset(), repr=False)
    unmatched_cancellations: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def create(cls, settings: Mapping[str, object]) -> IgnoreSpec:
        """Read the ignore list out of parsed settings, or raise :class:`IgnoreListMalformed`.

        A list that is not an array of strings is refused rather than read leniently: a lenient
        reading would carry less than the user asked for, and the difference is a value that gets
        sealed (``AGENTS.md`` §3.4).
        """
        entries: list[str] = []
        for name in (IGNORED_SETTINGS_KEY, LEGACY_IGNORED_SETTINGS_KEY):
            if name not in settings:
                continue
            value = settings[name]
            if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
                raise IgnoreListMalformed("the settings ignore list is not a list of strings")
            entries.extend(value)
        added = {entry for entry in entries if not entry.startswith(_CANCEL)}
        cancelled = {entry[len(_CANCEL) :] for entry in entries if entry.startswith(_CANCEL)}
        return cls(
            keys=frozenset((added - cancelled) - NON_IGNORABLE),
            unmatched_cancellations=tuple(sorted(cancelled - added)),
        )

    def paths_for(self, policy: PresetFile) -> frozenset[KeyPath]:
        """The key paths to carry for `policy`'s file: the user's keys minus whatever the preset strips.

        A key outside the preset's allowlist is already local, so carrying it would add nothing.
        """
        stripped = policy.stripped
        synced = policy.synced
        return frozenset(
            path
            for path in ((key,) for key in self.keys)
            if path not in stripped and (synced is None or any(path[: len(root)] == root for root in synced))
        )


NO_IGNORES: Final = IgnoreSpec()

__all__ = [
    "IGNORED_SETTINGS_KEY",
    "LEGACY_IGNORED_SETTINGS_KEY",
    "NON_IGNORABLE",
    "NO_IGNORES",
    "IgnoreSpec",
]
