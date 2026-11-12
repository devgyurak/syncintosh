# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings this machine keeps to itself: the preset's noise, the user's own ignore list, and ``-x``.

VS Code's ``settingsSync.ignoredSettings`` names settings that a machine does not share. VS Code
does not delete an ignored key from what it uploads; it leaves the remote's value in place
(``updateIgnoredSettings(content, remoteContent, ignored)``). syncintosh does the same, which is
why this is a *carry* and not a strip (:mod:`syncintosh.core.projection`): stripping the key would
make the 3-way merge read it as deleted and erase the value from every other machine.

The preset's own noise keys are VS Code's **default** ignore set, so they are carried the same way and
the list can subtract from them. ``PresetFile.stripped`` therefore holds only what a file excludes.

This module only reads the list. It takes the **parsed** settings (``AGENTS.md`` §2.1), so it
holds no file, adapter, or I/O, and returns the top-level keys to carry plus the entries it could
not honour.

* ``settingsSync.ignoredSettings`` and its legacy name ``sync.ignoredSettings`` are both read and
  their entries pooled.
* The ignored set is ``(noise ∪ the user's keys) − cancelled − NON_IGNORABLE``. An entry ``-x`` cancels
  ``x`` wherever it came from, the preset's noise included, which re-enables the key: it syncs again.
* An excluded key is stripped whatever the list says, so a ``-x`` aimed at one is reported, as a count.
  A ``-x`` that matches nothing is not: the key it names already syncs.
* ``settingsSync.ignoredSettings`` and ``settingsSync.ignoredExtensions`` cannot be ignored
  (``disallowSyncIgnore`` upstream), so the list itself does travel.
* Matching is by top-level key: ``"[python]"`` is one key, and so is ``"editor.fontSize"``. A noise entry of an
  ignore-list file is such a key (``PresetFile.create`` refuses any other), so ``-x`` names it exactly.
* Until a key has a value in the repo, carrying it leaves it out of the sealed object, which is what stripping
  does; objects of a repo that never held the key are byte-identical under both.
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

    `keys` are the user's ignored keys and `cancelled` the keys a ``-x`` entry names, both before the preset's
    policy is applied; :meth:`paths_for` applies it and :meth:`blocked_cancellations` counts the ``-x`` entries it
    cannot honour (``docs/design/presets.md``).
    """

    # Setting names come from the user's file, so a repr never shows them (``AGENTS.md`` §4).
    keys: frozenset[str] = field(default=frozenset(), repr=False)
    cancelled: frozenset[str] = field(default=frozenset(), repr=False)

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
        return cls(
            keys=frozenset(entry for entry in entries if not entry.startswith(_CANCEL)),
            cancelled=frozenset(entry[len(_CANCEL) :] for entry in entries if entry.startswith(_CANCEL)),
        )

    def paths_for(self, policy: PresetFile) -> frozenset[KeyPath]:
        """The key paths to carry for `policy`'s file: ``(noise ∪ the user's keys) − cancelled − NON_IGNORABLE``.

        A key the file excludes is stripped instead, so it is never carried; and a key outside the file's
        allowlist is already local, so carrying it would add nothing.
        """
        noise = {segment[0] for segment in policy.noise if len(segment) == 1 and isinstance(segment[0], str)}
        excluded = policy.excluded
        synced = policy.synced
        return frozenset(
            path
            for path in ((key,) for key in (noise | self.keys) - self.cancelled - NON_IGNORABLE)
            if path not in excluded and (synced is None or any(path[: len(root)] == root for root in synced))
        )

    def blocked_cancellations(self, policy: PresetFile) -> int:
        """How many ``-x`` entries name a key `policy`'s file excludes, which a list cannot switch back on."""
        return sum(1 for key in self.cancelled if (key,) in policy.excluded)


NO_IGNORES: Final = IgnoreSpec()

__all__ = [
    "IGNORED_SETTINGS_KEY",
    "LEGACY_IGNORED_SETTINGS_KEY",
    "NON_IGNORABLE",
    "NO_IGNORES",
    "IgnoreSpec",
]
