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


def _addresses(merged: Any) -> set[tuple[Any, Any]]:
    return {(conflict.path, conflict.element) for conflict in merged.conflicts}


def _pick_some(rng: random.Random, first: Any, skip: float) -> tuple[dict[Any, Any], dict[Any, Any]]:
    """A side or a proposal for some of the conflicts of `first`; the rest are left open with probability `skip`."""
    choices: dict[tuple[str, ...], Any] = {}
    element_picks: dict[Any, Any] = {}
    for conflict in first.conflicts:
        if rng.random() < skip:
            continue
        side = rng.choice(["local", "remote"])
        if conflict.element is None:
            choices[conflict.path] = side
        else:
            key = conflict.element[0][1]
            proposal = tree_from_value([{"key": key, "command": "proposed"}]).value(())
            element_picks[(conflict.path, conflict.element)] = proposal if rng.random() < 0.3 else side
    return choices, element_picks


def _pick_case(seed: int) -> tuple[bool, list[tuple[Any, Any]]]:
    """One random merge with some conflicts picked: whether a pick applied, and the addresses added."""
    rng = random.Random(seed)
    base, local, remote = (tree_from_value(_random_doc(rng)) for _ in range(3))
    first = merge_tree(base, local, remote, keyed=_DOC_KEYED)
    choices, element_picks = _pick_some(rng, first, skip=0.5)
    second = merge_tree(
        base, local, remote, keyed=_DOC_KEYED, choices=choices or None, element_picks=element_picks or None
    )
    before, after = _addresses(first), _addresses(second)
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


def _perturb(rng: random.Random, node: Any) -> Any:
    """A changed copy of a document: bindings gain args or go, arrays reorder, and scalars change.

    Both sides are made this way from one document, so the same binding is often changed on both sides and the merge
    has group conflicts to pick from, at the top level and one level down.
    """
    if isinstance(node, dict):
        return {key: _perturb(rng, value) for key, value in node.items()}
    if isinstance(node, list) and node and isinstance(node[0], dict):
        bindings = [dict(item) for item in node if rng.random() > 0.15]
        for binding in bindings:
            if rng.random() < 0.5:
                binding["args"] = rng.choice([1, 2])
        if rng.random() < 0.3:
            rng.shuffle(bindings)
        return bindings
    return rng.choice([1, 2, "x"]) if rng.random() < 0.5 else node


def _bindings(rng: random.Random) -> list[dict[str, object]]:
    return [{"key": key, "command": command} for key, command in rng.sample(_DOC_POOL, rng.randint(1, 3))]


def _related_doc(rng: random.Random) -> dict[str, object]:
    """A document whose arrays always hold bindings, at the top level and one level down, plus a scalar, so both sides
    have bindings and values to change and group and key conflicts are common."""
    doc: dict[str, object] = {
        "a": _bindings(rng),
        "b": _bindings(rng),
        "d": rng.choice([1, 2]),
        "e": rng.choice([1, 2]),
    }
    doc["c"] = {"a": _bindings(rng), "b": _bindings(rng)} if rng.random() < 0.5 else rng.choice([1, 2])
    return doc


def _related_case(seed: int) -> tuple[bool, bool, bool, bool, list[tuple[Any, Any]]]:
    """One merge of two changed copies of a document, with some of its conflicts picked, and some left open.

    Returns whether a group pick resolved at the top level, whether one resolved one level down, whether a key pick
    resolved (a whole value, not a group), whether a pick resolved while another conflict stayed open (a partial pick),
    and the addresses the merge added. A pick counts only when its own address is gone after the picks.
    """
    rng = random.Random(seed)
    document = _related_doc(rng)
    base = tree_from_value(document)
    local = tree_from_value(_perturb(rng, document))
    remote = tree_from_value(_perturb(rng, document))
    first = merge_tree(base, local, remote, keyed=_DOC_KEYED)
    choices, element_picks = _pick_some(rng, first, skip=0.4)
    second = merge_tree(
        base, local, remote, keyed=_DOC_KEYED, choices=choices or None, element_picks=element_picks or None
    )
    before, after = _addresses(first), _addresses(second)
    picked = set(element_picks) | {(path, None) for path in choices}
    resolved = picked & before - after
    top = any(len(path) == 1 for path, _ in resolved)
    nested = any(len(path) == 2 for path, _ in resolved)
    key = any(element is None for _, element in resolved)
    partial = bool(resolved) and bool(after)
    return top, nested, key, partial, sorted(after - before, key=repr)


@settings(max_examples=300, deadline=None)
@given(seed=st.integers(min_value=0, max_value=10**6))
def test_a_pick_never_adds_an_address_when_both_sides_change_the_same_bindings(seed: int) -> None:
    # when — both sides change shared bindings, some conflicts are picked and some are left open
    *_, added = _related_case(seed)

    # then — no address appears that the merge did not already have
    assert added == []


def test_the_related_cases_resolve_top_level_nested_key_and_partial_picks_in_many_documents() -> None:
    # when — a fixed run of documents, so the counts are the same on every run
    cases = [_related_case(seed) for seed in range(400)]

    # then — picks that resolve their own conflict apply at each level and alongside an open conflict, in many documents
    assert sum(top for top, _, _, _, _ in cases) >= 100
    assert sum(nested for _, nested, _, _, _ in cases) >= 50
    assert sum(key for _, _, key, _, _ in cases) >= 30
    assert sum(partial for _, _, _, partial, _ in cases) >= 50
