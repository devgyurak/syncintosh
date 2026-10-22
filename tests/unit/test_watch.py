# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure half of file watching: the filter and the debounce.

The debounce is driven with plain numbers (its properties are arithmetic) and the filter with
`FileEvent`s, so neither needs a filesystem or a clock.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.config import LocalGlob, LocalItem, SyncConfig
from syncintosh.core.watch import Debounce, FileEvent, WatchFilter

# Integer-valued seconds keep `now + gap - now == gap` exact, so an equality assertion is honest.
_seconds = st.integers(min_value=0, max_value=10**6).map(float)


def _config() -> SyncConfig:
    return SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(LocalItem.create(path=".zshrc", preset="zsh", scope="common", format="text"),),
        globs=(LocalGlob.create(path="Code/User/snippets/*.json", preset="vscode", scope="common", format="jsonc"),),
    )


@given(now=_seconds, quiet=_seconds, gap=_seconds)
def test_due_only_after_the_quiet_window(now, quiet, gap) -> None:
    # given — a maximum window far away, so the quiet window is what the property is about
    debounce = Debounce(quiet_seconds=quiet, max_seconds=10**9)

    # when
    debounce.touch(now)

    # then — due exactly when the elapsed time reaches the window
    assert debounce.due(now + gap) == (gap >= quiet)


def test_a_burst_that_never_goes_quiet_still_runs_at_the_maximum() -> None:
    # given — a directory under constant churn: events keep arriving inside the quiet window
    debounce = Debounce(quiet_seconds=1000.0, max_seconds=5.0)
    debounce.touch(0.0)
    debounce.touch(4.0)

    # when & then — the quiet window has not passed, but the maximum one has
    assert debounce.due(4.9) is False
    assert debounce.due(5.0) is True


def test_an_untouched_debounce_is_never_due() -> None:
    # given
    debounce = Debounce(quiet_seconds=1.0)

    # when & then
    assert debounce.due(1_000_000.0) is False


@given(first=_seconds, second=_seconds)
def test_a_later_touch_restarts_the_window(first, second) -> None:
    # given — a maximum window far away, so the quiet window is what the property is about
    debounce = Debounce(quiet_seconds=5.0, max_seconds=10**9)

    # when
    debounce.touch(first)
    debounce.touch(first + second)

    # then — the window is measured from the later event, not the first
    assert debounce.due(first + second + 5.0) is True
    assert debounce.due(first + second + 4.0) is False


def test_clear_makes_it_not_due_until_the_next_touch() -> None:
    # given
    debounce = Debounce(quiet_seconds=1.0)
    debounce.touch(0.0)

    # when
    debounce.clear()

    # then
    assert debounce.due(10.0) is False


def test_the_filter_matches_an_exact_item() -> None:
    # given
    targets = WatchFilter.create(_config())

    # when & then
    assert targets.matches(FileEvent(".zshrc")) is True
    assert targets.matches(FileEvent(".zprofile")) is False


def test_the_filter_matches_a_glob_target() -> None:
    # given
    targets = WatchFilter.create(_config())

    # when & then
    assert targets.matches(FileEvent("Code/User/snippets/javascript.json")) is True
    assert targets.matches(FileEvent("Code/User/settings.json")) is False


def test_the_filter_matches_a_rename_destination() -> None:
    # given — an atomic save writes a temp file and renames it onto the real path, so the *source* of
    # the event is a name no preset tracks
    targets = WatchFilter.create(_config())

    # when & then
    assert targets.matches(FileEvent(path=".zshrc.tmp", dest=".zshrc")) is True
    assert targets.matches(FileEvent(path=".zshrc.tmp")) is False


def test_the_filter_lists_the_directories_to_observe() -> None:
    # given
    targets = WatchFilter.create(_config())

    # when & then — an exact item at the home root watches `$HOME` (the empty string)
    assert targets.directories() == ("", "Code/User/snippets")


def test_the_filter_keeps_the_original_directory_spelling() -> None:
    # given — a path whose directory has capitals, the shape a lower-cased lookup would break
    config = SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(
            LocalItem.create(
                path="Library/Application Support/Code/User/settings.json",
                preset="vscode",
                scope="common",
                format="jsonc",
            ),
        ),
    )

    # when
    targets = WatchFilter.create(config)

    # then — the directory is scheduled under the name the filesystem reports events with
    assert targets.directories() == ("Library/Application Support/Code/User",)


def test_the_filter_matches_a_case_variant_of_an_item() -> None:
    # given — APFS is case-insensitive, so a differently-cased event names the same file
    targets = WatchFilter.create(_config())

    # when & then
    assert targets.matches(FileEvent(".Zshrc")) is True
