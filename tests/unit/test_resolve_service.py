# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the decisions `resolve --take` builds from the conflicts a cycle found."""

from __future__ import annotations

import pytest

from syncintosh.core.resolution import ConflictKind, ConflictView, Side
from syncintosh.services.resolve import take_decision

FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def _view(
    kind: ConflictKind,
    *,
    path: str = ".zshrc",
    fingerprint: str = "fp",
    local_present: bool = True,
    remote_present: bool = True,
) -> ConflictView:
    return ConflictView(
        kind=kind,
        file_id=FILE_ID,
        path=path,
        key_path=None,
        fingerprint=fingerprint,
        local_present=local_present,
        remote_present=remote_present,
    )


@pytest.mark.parametrize(
    ("kind", "side", "expected"),
    [
        ("file", "local", "local"),
        ("file", "remote", "remote"),
        ("key", "remote", "remote"),
        ("add/add", "local", "local"),
        ("modify/modify", "remote", "remote"),
        ("collision", "local", "local"),
    ],
)
def test_taking_a_side_picks_that_side_for_each_conflict_kind(kind: ConflictKind, side: Side, expected: str) -> None:
    # given
    view = _view(kind)
    # when
    picks = take_decision(side)((view,))
    # then
    assert picks == {"fp": expected}


@pytest.mark.parametrize(
    ("side", "local_present", "remote_present", "expected"),
    [
        ("local", True, False, "keep"),
        ("local", False, True, "delete"),
        ("remote", True, False, "delete"),
        ("remote", False, True, "keep"),
    ],
)
def test_taking_a_side_on_a_delete_modify_keeps_or_deletes_by_that_sides_presence(
    side: Side, local_present: bool, remote_present: bool, expected: str
) -> None:
    # given
    view = _view("delete/modify", local_present=local_present, remote_present=remote_present)
    # when
    picks = take_decision(side)((view,))
    # then
    assert picks == {"fp": expected}


def test_a_path_filter_takes_only_the_conflicts_on_those_paths() -> None:
    # given
    views = (_view("file", path=".zshrc", fingerprint="kept"), _view("file", path=".gitconfig", fingerprint="other"))
    # when
    picks = take_decision("remote", paths=(".zshrc",))(views)
    # then
    assert picks == {"kept": "remote"}


def test_no_conflict_on_the_named_paths_makes_no_decision() -> None:
    # given
    views = (_view("file", path=".zshrc"),)
    # when
    picks = take_decision("remote", paths=(".gitconfig",))(views)
    # then
    assert picks is None


def test_a_declined_approval_makes_no_decision() -> None:
    # given
    views = (_view("file"),)
    # when
    picks = take_decision("remote", approve=lambda chosen, decided: False)(views)
    # then
    assert picks is None


def test_an_approval_is_asked_with_only_the_conflicts_it_settles() -> None:
    # given
    views = (_view("file", path=".zshrc", fingerprint="kept"), _view("file", path=".gitconfig", fingerprint="other"))
    asked: list[tuple[str, ...]] = []

    def approve(chosen, decided):
        asked.append(tuple(view.path for view in chosen))
        return True

    # when
    picks = take_decision("local", paths=(".zshrc",), approve=approve)(views)
    # then
    assert asked == [(".zshrc",)]
    assert picks == {"kept": "local"}
