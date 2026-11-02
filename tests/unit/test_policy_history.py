# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shipped policy history must end at each built-in preset's current policy (issue #24)."""

from __future__ import annotations

import pytest

from syncintosh.core.policy import policy_digest
from syncintosh.presets import BUILTIN_PRESETS, BuiltinCatalog
from syncintosh.presets.history import POLICY_HISTORY

_HOW_TO_FIX = (
    "{name}: the preset's policy changed. Append this digest to POLICY_HISTORY[{name!r}] in "
    "src/syncintosh/presets/history.py (append, never edit an earlier entry): {digest}"
)


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda preset: preset.name)
def test_the_history_ends_at_the_current_digest(preset) -> None:
    # given
    digest = policy_digest(preset)

    # when
    history = POLICY_HISTORY.get(preset.name, ())

    # then
    assert history[-1:] == (digest,), _HOW_TO_FIX.format(name=preset.name, digest=digest)


def test_every_built_in_preset_has_a_history() -> None:
    # when
    missing = [preset.name for preset in BUILTIN_PRESETS if not POLICY_HISTORY.get(preset.name)]

    # then
    assert missing == []


def test_no_history_names_a_preset_that_is_not_built_in() -> None:
    # when
    stray = sorted(set(POLICY_HISTORY) - {preset.name for preset in BUILTIN_PRESETS})

    # then
    assert stray == []


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda preset: preset.name)
def test_a_history_never_repeats_a_digest(preset) -> None:
    # given — a repeated digest would make two revisions indistinguishable by their stamp
    history = POLICY_HISTORY[preset.name]

    # when
    repeated = len(history) - len(set(history))

    # then
    assert repeated == 0


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda preset: preset.name)
def test_the_catalog_serves_the_shipped_history(preset) -> None:
    # when
    history = BuiltinCatalog.default().policy_history(preset.name)

    # then
    assert history == POLICY_HISTORY[preset.name]
