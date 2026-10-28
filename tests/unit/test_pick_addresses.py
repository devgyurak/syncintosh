# SPDX-License-Identifier: AGPL-3.0-or-later
"""A pick never adds a conflict address; a pick whose render is refused names no address and says why."""

from __future__ import annotations

import random
from typing import Any, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import CHANGED_WITH_NOTE_REFUSED, JsoncAdapter
from syncintosh.core.content_merge import merge_content
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.merge import merge_tree
from syncintosh.core.tree import tree_from_value

RULE = ArrayRule.create(identity=("key", "command", "when"), required=("key", "command"), group=("key", "when"))
POOL = [("ctrl+k", "p"), ("ctrl+k", "q"), ("ctrl+m", "r"), ("ctrl+m", "s"), ("ctrl+z", "t")]


@st.composite
def _side(draw: st.DrawFn, base: list[tuple[str, str]]) -> list[dict[str, object]]:
    """A side over the base bindings: some of them, in a random order, with random args."""
    chosen = draw(st.lists(st.sampled_from(base), unique=True, max_size=len(base)))
    order = draw(st.permutations(chosen))
    args = draw(st.lists(st.sampled_from([None, 1]), min_size=len(order), max_size=len(order)))
    return [
        {"key": key, "command": command} | ({} if value is None else {"args": value})
        for (key, command), value in zip(order, args, strict=True)
    ]


def _pick(data: st.DataObject, element: Any) -> Any:
    """A side or a proposed binding for the group `element` (the group of a key with no `when`)."""
    key = element[0][1]
    proposal = tree_from_value([{"key": key, "command": "proposed"}]).value(())
    return {"local": "local", "remote": "remote", "proposal": proposal}[
        data.draw(st.sampled_from(["local", "remote", "proposal"]))
    ]


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_a_group_pick_or_proposal_never_adds_a_conflict_address(data: st.DataObject) -> None:
    # given — three arrays of bindings, the two sides changing the shared ones in different ways
    base_pairs = data.draw(st.lists(st.sampled_from(POOL), unique=True, min_size=2, max_size=len(POOL)))
    base = tree_from_value([{"key": key, "command": command} for key, command in base_pairs])
    local = tree_from_value(data.draw(_side(base_pairs)))
    remote = tree_from_value(data.draw(_side(base_pairs)))
    first = merge_tree(base, local, remote, keyed={(): RULE})
    before = {(conflict.path, conflict.element) for conflict in first.conflicts}

    # when — every conflicting group is given a side or a proposal, and the merge runs again
    groups = {conflict.element for conflict in first.conflicts if conflict.element is not None}
    element_picks: dict[Any, Any] = {((), group): _pick(data, group) for group in groups}
    second = merge_tree(base, local, remote, keyed={(): RULE}, element_picks=element_picks)

    # then — the addresses after the picks are no more than the addresses before them
    after = {(conflict.path, conflict.element) for conflict in second.conflicts}
    assert after <= before


def test_a_pick_whose_render_is_refused_names_no_address_and_gives_the_fixed_reason() -> None:
    # given — `a` is changed on both sides, and the local copy holds a comment inside it
    base = b'[\n  { "key": "a", "command": "x" }\n]\n'
    local = b'[\n  { "key": "a", "command": "x", /* why */ "args": 1 }\n]\n'
    remote = b'[\n  { "key": "a", "command": "x", "args": 2 }\n]\n'
    group = (("key", "a"), ("when", None))

    # when — the remote side is picked for that group, so its element must be written into the local span
    merged = merge_content(
        base,
        local,
        remote,
        cast(Any, None),
        JsoncAdapter(),
        arrays={(): RULE},
        element_picks={((), group): "remote"},
    )

    # then — the file stays in conflict, names no key path, and says why with the adapter's fixed text
    assert merged.conflicted is True
    assert merged.conflict_addresses == ()
    assert merged.reason == CHANGED_WITH_NOTE_REFUSED


_DOC_KEYS = ["a", "b", "c"]
_DOC_POOL = [("ctrl+k", "p"), ("ctrl+k", "q"), ("ctrl+m", "r"), ("ctrl+z", "t")]
_DOC_KEYED: dict[tuple[str, ...], ArrayRule] = {(key,): RULE for key in _DOC_KEYS} | {
    (parent, key): RULE for parent in _DOC_KEYS for key in _DOC_KEYS
}


def _random_doc(rng: random.Random, depth: int = 0) -> dict[str, object]:
    """A document with scalars, an object (one level down), and arrays of bindings at the top and one level down."""
    doc: dict[str, object] = {}
    for key in rng.sample(_DOC_KEYS, rng.randint(0, 3)):
        roll = rng.random()
        if depth == 0 and roll < 0.25:
            doc[key] = _random_doc(rng, 1)
        elif roll < 0.55:
            doc[key] = rng.choice([1, 2, "x"])
        else:
            doc[key] = [{"key": k, "command": c} for k, c in rng.sample(_DOC_POOL, rng.randint(0, 3))]
    return doc


def _pick_case(seed: int) -> tuple[bool, list[tuple[Any, Any]]]:
    """One random merge with some conflicts picked: whether a pick applied, and the addresses added."""
    rng = random.Random(seed)
    base, local, remote = (tree_from_value(_random_doc(rng)) for _ in range(3))
    first = merge_tree(base, local, remote, keyed=_DOC_KEYED)
    before = {(conflict.path, conflict.element) for conflict in first.conflicts}
    choices: dict[tuple[str, ...], Any] = {}
    element_picks: dict[Any, Any] = {}
    for conflict in first.conflicts:
        if rng.random() < 0.5:
            continue
        side = rng.choice(["local", "remote"])
        if conflict.element is None:
            choices[conflict.path] = side
        else:
            key = conflict.element[0][1]
            proposal = tree_from_value([{"key": key, "command": "proposed"}]).value(())
            element_picks[(conflict.path, conflict.element)] = proposal if rng.random() < 0.3 else side
    second = merge_tree(
        base, local, remote, keyed=_DOC_KEYED, choices=choices or None, element_picks=element_picks or None
    )
    after = {(conflict.path, conflict.element) for conflict in second.conflicts}
    return bool(before) and bool(choices or element_picks), sorted(after - before, key=repr)


@settings(max_examples=300, deadline=None)
@given(seed=st.integers(min_value=0, max_value=10**6))
def test_a_pick_of_a_key_or_a_group_never_adds_an_address_in_nested_documents(seed: int) -> None:
    # when — a random merge of documents with scalars, an object, and arrays, with some of its conflicts picked
    _, added = _pick_case(seed)

    # then — no address appears that the merge did not already have
    assert added == []


def test_the_nested_pick_property_is_exercised_by_many_documents() -> None:
    # when — a fixed run of documents, so the count is the same on every run
    cases = [_pick_case(seed) for seed in range(400)]

    # then — many cases have a conflict that a pick settled, and none adds an address
    assert sum(exercised for exercised, _ in cases) > 100
    assert [seed for seed, (_, added) in enumerate(cases) if added] == []
