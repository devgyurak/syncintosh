# SPDX-License-Identifier: AGPL-3.0-or-later
"""Property tests for re-enabling a noise key with ``-x`` (issue #73).

Noise of an ignore-list file is carried, not stripped, so ``-x`` can switch a key back on. Two things must hold
for that to be safe: a repo that never held the key seals exactly what the old strip sealed, and a key that is
switched on survives the project -> restore round trip (``AGENTS.md`` §5.3).
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.ignore import IGNORED_SETTINGS_KEY, IgnoreSpec
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import Carry, project_text, restore_text

ADAPTER = JsoncAdapter()
NOISE = {("zoom",), ("layout",)}
EXCLUDED = {("secret",)}
# The same policy before the change: noise stripped statically, and no ignore list at all.
STRIPPING = PresetFile.create(path="settings.json", format="jsonc", noise=NOISE, excluded=EXCLUDED)
CARRYING = PresetFile.create(path="settings.json", format="jsonc", noise=NOISE, excluded=EXCLUDED, ignore_list=True)

_LEAVES = st.one_of(st.integers(), st.booleans(), st.text(max_size=6))
_MEMBERS = st.dictionaries(st.sampled_from(["a", "b", "zoom", "layout", "secret"]), _LEAVES, max_size=5)
_INDENT = st.sampled_from([None, 2, 4])
# A file with no list still carries its noise.
NO_IGNORES_PATHS = IgnoreSpec.create({}).paths_for(CARRYING)


def _text(members: dict[str, object], indent: int | None) -> str:
    return json.dumps(members, indent=indent) + "\n"


@given(members=_MEMBERS, indent=_INDENT)
def test_a_repo_that_never_held_the_noise_gets_the_bytes_the_static_strip_gave(members, indent) -> None:
    # given
    live = _text(members, indent)
    carried = NO_IGNORES_PATHS

    # when
    carrying = project_text(ADAPTER, live, CARRYING, Carry(carried, None))
    stripping = project_text(ADAPTER, live, STRIPPING)

    # then
    assert carrying == stripping


@given(zoom=_LEAVES, own=_LEAVES, other=_MEMBERS, indent=_INDENT)
def test_a_switched_on_noise_key_survives_the_round_trip(zoom, own, other, indent) -> None:
    # given — `-zoom` switches the key on, so only `layout` is still carried
    spec = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["-zoom"]})
    carried = spec.paths_for(CARRYING)
    repo = project_text(ADAPTER, _text({**other, "zoom": zoom}, indent), CARRYING, Carry(carried, None))
    live = _text({**other, "zoom": own}, indent)

    # when
    restored = restore_text(ADAPTER, repo, live, CARRYING, Carry(carried, repo))

    # then — the repo's value is applied, and the result projects back to the object
    assert json.loads(restored)["zoom"] == zoom
    assert project_text(ADAPTER, restored, CARRYING, Carry(carried, repo)) == repo


@given(members=_MEMBERS, indent=_INDENT)
def test_a_switched_on_noise_key_is_sealed_and_an_excluded_one_never_is(members, indent) -> None:
    # given
    carried = IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["-zoom", "-secret"]}).paths_for(CARRYING)

    # when
    sealed = json.loads(project_text(ADAPTER, _text(members, indent), CARRYING, Carry(carried, None)))

    # then — the noise key is sealed whenever it is there, the credential never
    assert ("secret" in sealed, ("zoom" in sealed) == ("zoom" in members)) == (False, True)
