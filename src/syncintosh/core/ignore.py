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

import hashlib
from collections.abc import Iterable, Mapping
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
        return frozenset(
            path
            for path in ((key,) for key in (noise_keys(policy) | self.keys) - self.cancelled)
            if _carriable(path, policy)
        )

    def blocked_cancellations(self, policy: PresetFile) -> int:
        """How many ``-x`` entries name a key `policy`'s file excludes, which a list cannot switch back on."""
        return sum(1 for key in self.cancelled if (key,) in policy.excluded)


def noise_keys(policy: PresetFile) -> frozenset[str]:
    """The top-level keys `policy`'s noise names: the file's default ignore set."""
    return frozenset(segment[0] for segment in policy.noise if len(segment) == 1 and isinstance(segment[0], str))


def _carriable(path: KeyPath, policy: PresetFile) -> bool:
    """True when `path` may be carried for `policy`'s file: not excluded, not un-ignorable, inside the allowlist."""
    synced = policy.synced
    return (
        path not in policy.excluded
        and path[0] not in NON_IGNORABLE
        and (synced is None or any(path[: len(root)] == root for root in synced))
    )


def carry_digest(key: str) -> str:
    """What the ledger stores for a carried key: the `sha256` of its name, hex.

    The name itself never reaches the ledger (``AGENTS.md`` §3.3). It is not a secret (the same Mac's settings file
    holds the name in the clear); it keeps names out of the state file.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def carried_digests(paths: Iterable[KeyPath]) -> tuple[str, ...]:
    """The sorted, de-duplicated `carry_digest` of each carried key, as `LedgerEntry.carried` holds them."""
    return tuple(sorted({carry_digest(path[0]) for path in paths}))


def adopted_paths(
    previous: tuple[str, ...] | None, policy: PresetFile, carried: frozenset[KeyPath], live_keys: Iterable[str]
) -> frozenset[KeyPath]:
    """The keys this Mac carried when it last settled the file and carries no more: they start to sync now.

    `previous` is the ledger's carried set; `None` is a ledger from before the field, which counts as the preset's
    noise (what the build that wrote it kept local). Only a name can be adopted, and the ledger holds digests, so a
    key is looked for among `live_keys` and the file's noise keys. An excluded or un-ignorable key, or one outside the
    allowlist, is never adopted: it never starts to sync.

    The caller gives an adopted key the repo's value if the repo has one and the live value if not, so a Mac that
    switches a key on neither overwrites the value another Mac already shared nor loses its own.
    """
    noise = noise_keys(policy)
    before = {carry_digest(key) for key in noise} if previous is None else set(previous)
    gone = before - {carry_digest(path[0]) for path in carried}
    if not gone:
        return frozenset()
    return frozenset(
        path
        for path in ((key,) for key in {*live_keys, *noise})
        if carry_digest(path[0]) in gone and path not in carried and _carriable(path, policy)
    )


NO_IGNORES: Final = IgnoreSpec()

__all__ = [
    "IGNORED_SETTINGS_KEY",
    "LEGACY_IGNORED_SETTINGS_KEY",
    "NON_IGNORABLE",
    "NO_IGNORES",
    "IgnoreSpec",
    "adopted_paths",
    "carried_digests",
    "carry_digest",
    "noise_keys",
]
