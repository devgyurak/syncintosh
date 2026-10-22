# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the apply-side "create or defer" rule and its report row."""

from __future__ import annotations

import pytest

from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.vscode import SETTINGS
from syncintosh.services.sync import NO_APP_INFO, _deferral, _missing_file_decision


@pytest.mark.parametrize(
    ("installed", "parent_exists", "expected"),
    [
        # the directory is already there: write, whatever the detector said — Spotlight can miss a
        # bundle the machine clearly has, and a `False` must not defer a file forever
        (True, False, "write"),
        (True, True, "write"),
        (False, True, "write"),
        # the detector says installed: create the parent directory too
        # (covered by the first row)
        (False, False, "defer"),
        # cannot tell: the directory heuristic, exactly as before app detection
        (None, True, "write"),
        (None, False, "defer"),
    ],
)
def test_the_create_or_defer_rule(installed, parent_exists, expected) -> None:
    # given — the three-way verdict app detection produces, per the design table
    # when
    decision = _missing_file_decision(installed, parent_exists=parent_exists)

    # then
    assert decision == expected


def test_a_not_installed_app_defers_with_its_download_metadata() -> None:
    # given — the local catalog's own metadata, never a record's untrusted preset name
    preset = BuiltinCatalog.default().get("vscode")

    # when
    row = _deferral(SETTINGS, preset, False)

    # then
    assert row.reason == "the app is not installed on this machine"
    assert row.download_url == "https://code.visualstudio.com/download"
    assert row.cask == "visual-studio-code"


def test_an_undeterminable_app_defers_without_metadata() -> None:
    # given — a detector that could not answer, or a path with no bundle ID
    preset = BuiltinCatalog.default().get("vscode")

    # when
    row = _deferral(SETTINGS, preset, None)

    # then — no app is being suggested, so no download page is shown
    assert row.download_url is None
    assert row.cask is None


def test_the_default_detector_never_claims_to_know() -> None:
    # when & then — a caller with no detector keeps the directory heuristic
    assert NO_APP_INFO.installed("com.microsoft.VSCode") is None
