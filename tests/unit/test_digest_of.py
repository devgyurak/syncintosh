# SPDX-License-Identifier: AGPL-3.0-or-later
"""A conflict's fingerprint must change with its values, even though a value's repr no longer shows it."""

from __future__ import annotations

from syncintosh.core.resolution import digest_of, fingerprint
from syncintosh.core.tree import ABSENT, Array, Scalar, Tree


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
