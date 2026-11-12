# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit and property tests for deciding that a VS Code profile was renamed (issue #67).

The layout is the shipped one (`presets/vscode.py`); every file name below is one it lists.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.profile_rename import (
    ANCHORS_DISAGREE,
    CONCURRENT_RENAME,
    NOT_ALLOWED,
    OLD_NAME_PRESENT,
    TARGET_EXISTS,
    UNREADABLE,
    ProfileMove,
    ProfileRenamePlan,
    RenameBlocked,
    RenamedElsewhere,
    moved_records,
    plan_profile_renames,
    profile_anchor,
)
from syncintosh.core.profiles import ProfileEntry, ProfileMap, profile_name_key
from syncintosh.presets import vscode

SPEC = vscode.PROFILES
DIR_A, DIR_B = "7c91e0aa", "-3f2a1b"
ID_1, ID_2, ID_3 = (f"{n:032x}" for n in (1, 2, 3))
NAMES = st.text(alphabet=st.characters(exclude_categories=["Cs"]), min_size=1, max_size=20)


def _allow_all(record: ManifestRecord) -> bool:
    return True


def _record(file_id: str, name: str, relative: str = "settings.json") -> ManifestRecord:
    return ManifestRecord.create(
        file_id=file_id,
        path=SPEC.logical_path(name, relative),
        preset="vscode",
        scope="common",
        machine=None,
        format="jsonc",
        sha256="a" * 64,
    )


def _relative(record: ManifestRecord) -> str:
    parsed = SPEC.parse(record.path)
    assert parsed is not None
    return parsed.relative


def _entry(directory: str, name: str) -> LedgerEntry:
    return LedgerEntry(
        sha256="b" * 64, commit="c" * 40, suppressed=False, profile_dir=directory, profile_key=profile_anchor(name)
    )


def _map(*named: tuple[str, str]) -> ProfileMap:
    return ProfileMap.create(tuple(ProfileEntry.create(name=name, directory=directory) for name, directory in named))


def _by_id(*records: ManifestRecord) -> dict[str, ManifestRecord]:
    return {record.file_id: record for record in records}


def _plan(
    records: Mapping[str, ManifestRecord], ledger: Mapping[str, LedgerEntry], profiles: ProfileMap, allows=_allow_all
) -> ProfileRenamePlan:
    return plan_profile_renames(records, ledger, profiles, SPEC, allows)


# --------------------------------------------------------------------------- anchor


@pytest.mark.parametrize("other", ["work", "WORK", "Work"])
def test_an_anchor_ignores_case_and_spelling(other: str) -> None:
    # when & then
    assert profile_anchor(other) == profile_anchor("Work")


def test_an_anchor_is_a_digest_that_does_not_hold_the_name() -> None:
    # when
    anchor = profile_anchor("Work")

    # then
    assert (len(anchor), "Work" in anchor, anchor != profile_anchor("Job")) == (64, False, True)


# --------------------------------------------------------------------------- a rename on this Mac


def test_a_profile_renamed_here_moves_all_its_records_including_one_without_a_baseline() -> None:
    # given
    records = _by_id(_record(ID_1, "Work"), _record(ID_2, "Work", "keybindings.json"))
    ledger = {ID_1: _entry(DIR_A, "Work")}

    # when
    plan = _plan(records, ledger, _map(("Job", DIR_A)))

    # then — ID_2 has no ledger entry (an unapplied borrowed resource) and still follows its profile
    assert plan == ProfileRenamePlan(
        moves=(
            ProfileMove(
                old="Work",
                new="Job",
                paths=(
                    (ID_1, SPEC.logical_path("Job", "settings.json")),
                    (ID_2, SPEC.logical_path("Job", "keybindings.json")),
                ),
            ),
        )
    )


def test_a_move_changes_the_path_and_nothing_else() -> None:
    # given
    records = _by_id(_record(ID_1, "Work"))
    plan = _plan(records, {ID_1: _entry(DIR_A, "Work")}, _map(("Job", DIR_A)))

    # when
    moved = moved_records(records, plan)

    # then
    assert moved == {ID_1: _record(ID_1, "Job")}


def test_a_profile_of_another_name_is_left_alone() -> None:
    # given
    records = _by_id(_record(ID_1, "Work"), _record(ID_2, "Play"))
    ledger = {ID_1: _entry(DIR_A, "Work"), ID_2: _entry(DIR_B, "Play")}

    # when
    moved = moved_records(records, _plan(records, ledger, _map(("Job", DIR_A), ("Play", DIR_B))))

    # then
    assert moved[ID_2] == records[ID_2]


# --------------------------------------------------------------------------- a rename on another Mac


def test_a_profile_renamed_elsewhere_is_reported_and_not_moved_back() -> None:
    # given — the records already say Job; this Mac's directory is still Work, and the ledger remembers Work
    records = _by_id(_record(ID_1, "Job"))

    # when
    plan = _plan(records, {ID_1: _entry(DIR_B, "Work")}, _map(("Work", DIR_B)))

    # then
    assert plan == ProfileRenamePlan(elsewhere=(RenamedElsewhere(old="Work", new="Job", directory=DIR_B),))


# --------------------------------------------------------------------------- nothing to do


@pytest.mark.parametrize(
    ("ledger", "profiles"),
    [
        pytest.param({}, _map(("Job", DIR_A)), id="no-ledger-entry"),
        pytest.param(
            {ID_1: LedgerEntry(sha256="b" * 64, commit="c" * 40, suppressed=False)},
            _map(("Job", DIR_A)),
            id="ledger-written-before-the-anchor-existed",
        ),
        pytest.param(
            {ID_1: LedgerEntry(sha256="b" * 64, commit="c" * 40, suppressed=False, profile_dir=DIR_A)},
            _map(("Job", DIR_A)),
            id="half-an-anchor",
        ),
        pytest.param({ID_1: _entry(DIR_A, "Work")}, _map(("work", DIR_A)), id="a-case-only-rename"),
        pytest.param({ID_1: _entry(DIR_A, "Work")}, _map(("Work", DIR_A)), id="not-renamed"),
        pytest.param({ID_1: _entry(DIR_A, "Work")}, _map(("Job", DIR_B)), id="the-directory-is-gone"),
        pytest.param({ID_1: _entry(DIR_A, "Play")}, _map(("Work", DIR_A)), id="converged-after-a-remote-rename"),
    ],
)
def test_no_evidence_of_a_rename_changes_nothing(ledger: dict[str, LedgerEntry], profiles: ProfileMap) -> None:
    # given
    records = _by_id(_record(ID_1, "Work"))

    # when
    plan = _plan(records, ledger, profiles)

    # then
    assert plan == ProfileRenamePlan()


def test_a_record_that_is_not_a_profile_path_is_never_matched() -> None:
    # given
    plain = ManifestRecord.create(
        file_id=ID_1, path=".zshrc", preset="zsh", scope="common", machine=None, format="text", sha256="a" * 64
    )

    # when
    plan = _plan({ID_1: plain}, {ID_1: _entry(DIR_A, "Work")}, _map(("Job", DIR_A)))

    # then
    assert plan == ProfileRenamePlan()


# --------------------------------------------------------------------------- blocked


@pytest.mark.parametrize(
    ("records", "ledger", "profiles", "allows", "reason"),
    [
        pytest.param(
            _by_id(_record(ID_1, "Work")),
            {ID_1: _entry(DIR_A, "Work")},
            ProfileMap.unreadable("storage.json is corrupt"),
            _allow_all,
            UNREADABLE,
            id="the-profile-list-is-unreadable",
        ),
        pytest.param(
            _by_id(_record(ID_1, "Work")),
            {ID_1: _entry(DIR_A, "Work")},
            _map(("Job", DIR_A)),
            lambda record: False,
            NOT_ALLOWED,
            id="the-new-name-is-not-in-the-config",
        ),
        pytest.param(
            _by_id(_record(ID_1, "Work"), _record(ID_2, "Job", "keybindings.json")),
            {ID_1: _entry(DIR_A, "Work")},
            _map(("Job", DIR_A)),
            _allow_all,
            TARGET_EXISTS,
            id="a-record-already-sits-under-the-new-name",
        ),
        pytest.param(
            _by_id(_record(ID_1, "Work"), _record(ID_2, "Work", "keybindings.json")),
            {ID_1: _entry(DIR_A, "Work"), ID_2: _entry(DIR_B, "Work")},
            _map(("Job", DIR_A), ("Play", DIR_B)),
            _allow_all,
            ANCHORS_DISAGREE,
            id="the-anchors-name-two-directories",
        ),
        pytest.param(
            _by_id(_record(ID_1, "Work")),
            {ID_1: _entry(DIR_A, "Work")},
            _map(("Job", DIR_A), ("Work", DIR_B)),
            _allow_all,
            OLD_NAME_PRESENT,
            id="another-directory-is-called-the-old-name",
        ),
        pytest.param(
            _by_id(_record(ID_1, "Job")),
            {ID_1: _entry(DIR_A, "Work")},
            _map(("Play", DIR_A)),
            _allow_all,
            CONCURRENT_RENAME,
            id="two-macs-chose-different-names",
        ),
    ],
)
def test_an_unsafe_rename_is_reported_and_moves_nothing(
    records: dict[str, ManifestRecord],
    ledger: dict[str, LedgerEntry],
    profiles: ProfileMap,
    allows,
    reason: str,
) -> None:
    # when
    plan = _plan(records, ledger, profiles, allows)

    # then
    assert (plan.moves, [blocked.reason for blocked in plan.blocked], moved_records(records, plan)) == (
        (),
        [reason],
        records,
    )


def test_two_profiles_renamed_to_one_name_both_stay() -> None:
    # given — both profiles' anchors name DIR_A, which is now called Job
    records = _by_id(_record(ID_1, "Work"), _record(ID_2, "Play"))
    ledger = {ID_1: _entry(DIR_A, "Work"), ID_2: _entry(DIR_A, "Play")}

    # when
    plan = _plan(records, ledger, _map(("Job", DIR_A)))

    # then
    assert (plan.moves, plan.blocked) == (
        (),
        (RenameBlocked("Play", ANCHORS_DISAGREE), RenameBlocked("Work", ANCHORS_DISAGREE)),
    )


def test_two_groups_anchored_to_one_directory_are_both_left_alone_even_when_one_has_not_moved() -> None:
    # given — "Work" was renamed to "Job" here, and "Job" records claim that directory too
    records = _by_id(_record(ID_1, "Work"), _record(ID_2, "Job"))
    ledger = {ID_1: _entry(DIR_A, "Work"), ID_2: _entry(DIR_A, "Job")}

    # when
    plan = _plan(records, ledger, _map(("Job", DIR_A)))

    # then
    assert (plan.moves, plan.elsewhere, [b.reason for b in plan.blocked]) == ((), (), [ANCHORS_DISAGREE] * 2)


def test_a_move_is_asked_about_the_records_as_they_would_be() -> None:
    # given
    records = _by_id(_record(ID_1, "Work"))
    asked: list[str] = []

    # when
    _plan(
        records, {ID_1: _entry(DIR_A, "Work")}, _map(("Job", DIR_A)), lambda record: asked.append(record.path) or True
    )

    # then
    assert asked == [SPEC.logical_path("Job", "settings.json")]


# --------------------------------------------------------------------------- properties


@st.composite
def _renamed_here(draw: st.DrawFn) -> tuple[str, str, list[str], dict[str, ManifestRecord], dict[str, LedgerEntry]]:
    """A profile `old` of 1–4 files, some with a baseline, that this Mac now calls `new`."""
    old, new = draw(NAMES), draw(NAMES)
    assume(profile_name_key(old) != profile_name_key(new))
    anchored = draw(st.lists(st.booleans(), min_size=1, max_size=4))
    assume(any(anchored))
    ids = [f"{n:032x}" for n in range(len(anchored))]
    files = ["settings.json", "keybindings.json", "snippets/a.json", "snippets/b.code-snippets"]
    records = {file_id: _record(file_id, old, files[n]) for n, file_id in enumerate(ids)}
    ledger = {file_id: _entry(DIR_A, old) for file_id, has in zip(ids, anchored, strict=True) if has}
    return old, new, ids, records, ledger


@given(_renamed_here())
def test_property_every_record_of_the_profile_moves_and_keeps_its_id(
    scenario: tuple[str, str, list[str], dict[str, ManifestRecord], dict[str, LedgerEntry]],
) -> None:
    # given
    _, new, ids, records, ledger = scenario

    # when
    moved = moved_records(records, _plan(records, ledger, _map((new, DIR_A))))

    # then — same count, same ids, every path under the new name
    assert (sorted(moved), [moved[i].path for i in ids]) == (
        sorted(records),
        [SPEC.logical_path(new, _relative(records[i])) for i in ids],
    )


@given(_renamed_here())
def test_property_planning_again_after_the_move_is_a_no_op(
    scenario: tuple[str, str, list[str], dict[str, ManifestRecord], dict[str, LedgerEntry]],
) -> None:
    # given
    _, new, _, records, ledger = scenario
    profiles = _map((new, DIR_A))
    moved = moved_records(records, _plan(records, ledger, profiles))

    # when
    again = _plan(moved, ledger, profiles)

    # then
    assert (again.moves, moved_records(moved, again)) == ((), moved)


@given(_renamed_here())
def test_property_an_unreadable_list_moves_nothing(
    scenario: tuple[str, str, list[str], dict[str, ManifestRecord], dict[str, LedgerEntry]],
) -> None:
    # given
    _, _, _, records, ledger = scenario

    # when
    plan = _plan(records, ledger, ProfileMap.unreadable("gone"))

    # then
    assert (plan.moves, moved_records(records, plan)) == ((), records)


@given(_renamed_here())
def test_property_the_other_mac_never_moves_the_records_back(
    scenario: tuple[str, str, list[str], dict[str, ManifestRecord], dict[str, LedgerEntry]],
) -> None:
    # given — what Mac A published; Mac B still calls its own directory `old` and remembers that in its ledger
    old, new, _, records, ledger = scenario
    published = moved_records(records, _plan(records, ledger, _map((new, DIR_A))))
    b_ledger = {file_id: _entry(DIR_B, old) for file_id in ledger}

    # when
    plan = _plan(published, b_ledger, _map((old, DIR_B)))

    # then
    assert (plan.moves, moved_records(published, plan)) == ((), published)
