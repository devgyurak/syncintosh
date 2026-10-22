# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the decision every resolve run uses, and the choice sources that feed it."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import ConflictsNotCovered
from syncintosh.core.resolution import ConflictKind, ConflictView, Side
from syncintosh.services.resolve import decision, normalize_path, scoped_chooser, take_chooser

FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
HOME = "/Users/someone"


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
    picks = decision(take_chooser(side))((view,), {})
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
    picks = decision(take_chooser(side))((view,), {})
    # then
    assert picks == {"fp": expected}


def test_a_conflict_already_answered_is_not_asked_again() -> None:
    # given
    asked: list[str] = []

    def choose(view: ConflictView) -> Side:
        asked.append(view.fingerprint)
        return "local"

    view = _view("file", fingerprint="new")
    # when
    picks = decision(choose)((view, _view("file", path=".gitconfig", fingerprint="old")), {"old": "remote"})
    # then — the answered conflict keeps its earlier pick and is not offered again
    assert asked == ["new"]
    assert picks == {"new": "local"}


def test_a_conflict_with_no_choice_stops_the_run_and_names_it() -> None:
    # given
    views = (_view("file", path=".zshrc"), _view("file", path=".gitconfig", fingerprint="other"))
    scoped = scoped_chooser(local=(".zshrc",), remote=(), home=HOME)
    # when & then — nothing is returned, so nothing can be published on a partial decision
    with pytest.raises(ConflictsNotCovered, match=r"\.gitconfig \(file\)"):
        decision(scoped)(views, {})


def test_an_approval_sees_every_pick_the_run_would_publish() -> None:
    # given
    views = (_view("file", fingerprint="old"), _view("file", path=".gitconfig", fingerprint="new"))
    seen: list[dict] = []

    def approve(shown, picks) -> bool:
        seen.append(dict(picks))
        return True

    # when
    picks = decision(take_chooser("remote"), approve=approve)(views, {"old": "local"})
    # then
    assert seen == [{"old": "local", "new": "remote"}]
    assert picks == {"new": "remote"}


def test_a_declined_approval_makes_no_decision() -> None:
    # given
    views = (_view("file"),)
    # when
    picks = decision(take_chooser("remote"), approve=lambda shown, picks: False)(views, {})
    # then
    assert picks is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".zshrc", ".zshrc"),
        ("./.zshrc", ".zshrc"),
        ("~/.zshrc", ".zshrc"),
        (f"{HOME}/.zshrc", ".zshrc"),
        ("dotfiles/.gitconfig", "dotfiles/.gitconfig"),
    ],
)
def test_a_path_is_matched_as_the_home_relative_path_a_conflict_records(raw: str, expected: str) -> None:
    # given — the forms a shell or a person may type for one tracked file
    # when
    normalized = normalize_path(raw, HOME)
    # then
    assert normalized == expected


def test_a_scoped_path_matches_a_conflict_whose_case_differs_on_a_case_folding_disk() -> None:
    # given — APFS folds case, so `.Zshrc` names the same tracked file as `.zshrc`
    views = (_view("file", path=".zshrc"),)
    # when
    picks = decision(scoped_chooser(local=(".Zshrc",), remote=(), home=HOME))(views, {})
    # then
    assert picks == {"fp": "local"}


def test_one_path_named_for_both_sides_is_refused() -> None:
    # when & then
    with pytest.raises(ValueError, match="both sides"):
        scoped_chooser(local=(".zshrc",), remote=("~/.zshrc",), home=HOME)
