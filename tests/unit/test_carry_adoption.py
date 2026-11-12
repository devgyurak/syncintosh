# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for adopting a key that stops being carried (issue #73).

A key this Mac carried at its last settle and carries no more starts to sync: it takes the repo's value if the repo
has one and keeps the live value if not. The ledger holds digests of the carried keys, never the names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import LedgerCorrupt
from syncintosh.core.ignore import adopted_paths, carried_digests, carry_digest
from syncintosh.core.models import LedgerEntry
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import Carry, project_text, restore_text
from syncintosh.presets import BuiltinCatalog, vscode
from syncintosh.services.sync._shared import _IGNORE_LIST_UNREADABLE, _Presets
from syncintosh.system.ledger import JsonLedgerStore

ADAPTER = JsoncAdapter()
POLICY = PresetFile.create(
    path="settings.json", format="jsonc", noise={("zoom",)}, excluded={("secret",)}, ignore_list=True
)
FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def _digests(*keys: str) -> tuple[str, ...]:
    return carried_digests((key,) for key in keys)


@pytest.mark.parametrize(
    ("previous", "carried", "live", "adopted"),
    [
        # Nothing stopped being carried.
        (_digests("zoom"), {("zoom",)}, ["zoom"], set()),
        (_digests("a", "zoom"), {("a",), ("zoom",)}, ["a", "zoom"], set()),
        # A ledger from before the field counts the preset's noise as carried.
        (None, set(), ["zoom", "a"], {("zoom",)}),
        (None, {("zoom",)}, ["zoom"], set()),
        # Switched on, or dropped from the user's list.
        (_digests("zoom"), set(), ["zoom"], {("zoom",)}),
        (_digests("a", "zoom"), {("zoom",)}, ["a", "zoom"], {("a",)}),
        # A noise key the live file does not have is still found, because its name is known.
        (_digests("zoom"), set(), [], {("zoom",)}),
        # An empty set is not a legacy ledger: nothing was carried, so nothing is adopted.
        ((), set(), ["zoom"], set()),
        # A key the ledger names that is in neither the live file nor the noise cannot be named, so it is left.
        (_digests("gone"), set(), ["a"], set()),
        # An excluded key never starts to sync.
        (_digests("secret"), set(), ["secret"], set()),
    ],
)
def test_the_adopted_keys_are_those_carried_before_and_not_now(previous, carried, live, adopted) -> None:
    # when
    result = adopted_paths(previous, POLICY, frozenset(carried), live)

    # then
    assert result == adopted


def test_a_digest_is_stable_and_hides_the_name() -> None:
    # when
    digest = carry_digest("window.zoomLevel")

    # then
    assert (digest, "zoomLevel" in digest) == (carry_digest("window.zoomLevel"), False)


def test_the_digests_are_sorted_and_de_duplicated() -> None:
    # when
    digests = carried_digests([("b",), ("a",), ("b",)])

    # then
    assert (digests, list(digests) == sorted(digests), len(digests)) == (_digests("a", "b"), True, 2)


@pytest.mark.parametrize("carried", [None, (), ("a" * 64,), ("a" * 64, "b" * 64)])
def test_the_carried_set_round_trips_through_the_ledger_file(tmp_path: Path, carried) -> None:
    # given
    store = JsonLedgerStore(tmp_path / "applied.json")
    entry = LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=False, carried=carried)

    # when
    store.save({FILE_ID: entry})

    # then — a legacy entry (`None`) stays legacy and an empty set stays empty
    assert store.load() == {FILE_ID: entry}


def test_an_entry_without_a_carried_set_is_not_written_with_one(tmp_path: Path) -> None:
    # given
    path = tmp_path / "applied.json"

    # when
    JsonLedgerStore(path).save({FILE_ID: LedgerEntry(sha256="a" * 64, commit="b" * 40, suppressed=False)})

    # then
    assert "carried" not in json.loads(path.read_text(encoding="utf-8"))["files"][FILE_ID]


@pytest.mark.parametrize("bad", ["abc", [1], {"a": 1}])
def test_a_malformed_carried_set_is_a_corrupt_ledger(tmp_path: Path, bad) -> None:
    # given
    path = tmp_path / "applied.json"
    entry = {"sha256": "a" * 64, "commit": "b" * 40, "suppressed": False, "carried": bad}
    path.write_text(json.dumps({"version": 1, "files": {FILE_ID: entry}}), encoding="utf-8")

    # when & then
    with pytest.raises(LedgerCorrupt):
        JsonLedgerStore(path).load()


def _text(**members: object) -> str:
    return json.dumps(members, indent=2) + "\n"


@pytest.mark.parametrize(
    ("live", "source", "expected"),
    [
        # The source has a value: it wins over the live one.
        (_text(zoom=2, a=1), _text(zoom=5, a=1), {"zoom": 5, "a": 1}),
        # The source lacks it: the live value stays.
        (_text(zoom=2, a=1), _text(a=1), {"zoom": 2, "a": 1}),
        # No source at all: the live value stays.
        (_text(zoom=2, a=1), None, {"zoom": 2, "a": 1}),
    ],
)
def test_an_adopted_key_takes_the_sources_value_or_keeps_the_live_one(live, source, expected) -> None:
    # when
    projected = project_text(ADAPTER, live, POLICY, Carry(frozenset(), source, frozenset({("zoom",)})))

    # then
    assert json.loads(projected) == expected


@given(live=st.integers(), repo=st.integers(), other=st.integers())
def test_restoring_an_object_writes_the_repos_value_for_an_adopted_key(live, repo, other) -> None:
    # given — the key is not carried, so apply puts the repo's value there whatever the live file holds
    repo_text = _text(zoom=repo, a=other)

    # when
    restored = restore_text(ADAPTER, repo_text, _text(zoom=live, a=other), POLICY, Carry(frozenset()))

    # then
    assert json.loads(restored) == {"zoom": repo, "a": other}


def test_a_list_that_could_not_be_read_still_carries_the_noise() -> None:
    # given — noise is no longer stripped, so a caller that skipped the ignore-list check must not seal it

    presets = _Presets(
        catalog=BuiltinCatalog.default(),
        adapters={},
        validators={},
        ignores={vscode.SETTINGS: _IGNORE_LIST_UNREADABLE},
    )
    policy = presets.policy_for(vscode.SETTINGS)

    # when
    carried = presets.carried(policy) if policy is not None else frozenset()

    # then
    assert carried == {("window.zoomLevel",), *({(key,) for (key,) in vscode.NOISE})}
