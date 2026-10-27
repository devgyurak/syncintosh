# SPDX-License-Identifier: AGPL-3.0-or-later
"""A conflict's fingerprint must change with its values, even though a value's repr no longer shows it."""

from __future__ import annotations

import os
import subprocess
import sys

from syncintosh.core.resolution import digest_of, fingerprint
from syncintosh.core.tree import ABSENT, Array, Object, Scalar, Tree


def test_two_different_values_at_one_path_have_different_digests() -> None:
    # given
    first = Scalar("str", "value-one")
    second = Scalar("str", "value-two")

    # when & then — the repr of both is "Scalar(kind='str', value=<hidden>)", so the digest must read the value
    assert digest_of(first) != digest_of(second)


def test_the_same_value_has_the_same_digest_each_time() -> None:
    # when & then
    assert digest_of(Scalar("str", "same")) == digest_of(Scalar("str", "same"))


def test_the_kind_is_part_of_the_digest() -> None:
    # when & then — `1` as an integer and `"1"` as a string are different settings
    assert digest_of(Scalar("int", 1)) != digest_of(Scalar("str", "1"))


def test_a_fingerprint_of_one_path_changes_when_its_value_does() -> None:
    # given
    before = digest_of(Tree.create({("editor", "fontSize"): Scalar("int", 13)}))
    after = digest_of(Tree.create({("editor", "fontSize"): Scalar("int", 15)}))

    # when & then
    assert fingerprint("f" * 32, ("editor", "fontSize"), before, before, before) != fingerprint(
        "f" * 32, ("editor", "fontSize"), before, after, before
    )


def test_an_array_and_an_absent_side_have_distinct_digests() -> None:
    # when & then
    assert digest_of(Array((Scalar("str", "a"),))) != digest_of(ABSENT)


def _nested_object(depth: int, leaf: str) -> Object:
    """An object nested `depth` levels deep, ending in a scalar `leaf`; built without recursion."""
    node: object = Scalar("str", leaf)
    for _ in range(depth):
        node = Object((("k", node),))
    assert isinstance(node, Object)
    return node


def test_a_deeply_nested_value_digests_without_exhausting_the_recursion_limit() -> None:
    # given — deeper than Python's recursion limit
    deep = _nested_object(5000, "one")

    # when & then — it digests, and a change at the bottom changes the digest
    assert digest_of(deep) != digest_of(_nested_object(5000, "two"))


def test_one_and_one_point_zero_and_true_and_the_string_one_all_digest_differently() -> None:
    # when & then — the kind is part of the value
    scalars = [Scalar("int", 1), Scalar("float", 1.0), Scalar("bool", True), Scalar("str", "1")]
    assert len({digest_of(scalar) for scalar in scalars}) == 4


def test_mapping_key_order_does_not_change_the_digest() -> None:
    # when & then — a mapping is a set of entries
    assert digest_of({"a": 1, "b": 2}) == digest_of({"b": 2, "a": 1})


def test_set_member_order_does_not_change_the_digest() -> None:
    # when & then
    assert digest_of({"x", "y", "z"}) == digest_of({"z", "y", "x"})


def test_a_tuple_and_a_list_with_the_same_members_digest_differently() -> None:
    # when & then
    assert digest_of((1, 2)) != digest_of([1, 2])


def test_bytes_and_their_text_digest_differently() -> None:
    # when & then
    assert digest_of(b"abc") != digest_of("abc")


def test_a_digest_is_the_same_under_every_hash_seed() -> None:
    # given — a mapping and a set, whose iteration order is the one that depends on hashing
    script = (
        "from syncintosh.core.resolution import digest_of\n"
        "print(digest_of({'a': 1, 'b': [2, 3], 'c': {'d', 'e', 'f'}}))\n"
    )
    digests = {
        seed: subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(6)
    }

    # when & then — the same value digests the same in every process
    assert len(set(digests.values())) == 1
