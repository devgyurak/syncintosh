# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the widened secret scan still passes every built-in preset's fixture (#28).

The scan gates the encrypt boundary, so a pattern that refuses a normal settings file stops the
user's whole machine (``AGENTS.md`` §6). A preset cuts its credential keys on the **collect** side,
so what the scan actually sees is the projection — this projects each fixture exactly as a collect
would and scans the result with the real scanner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.projection import project_text
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.docker import CONFIG, DAEMON
from syncintosh.presets.vscode import KEYBINDINGS, SETTINGS
from syncintosh.system.secret_scan import PatternSecretScanner

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# (preset path, fixture, format). The tabs fixture is a second `settings.json` sample, with CRLF.
CASES = (
    (SETTINGS, "jsonc/vscode.jsonc", "jsonc"),
    (SETTINGS, "jsonc/vscode-tabs-crlf.jsonc", "jsonc"),
    (KEYBINDINGS, "jsonc/vscode-keybindings.jsonc", "jsonc"),
    (CONFIG, "docker/config.json", "jsonc"),
    (DAEMON, "docker/daemon.json", "jsonc"),
)


@pytest.mark.parametrize(("path", "fixture", "fmt"), CASES)
def test_a_preset_fixture_passes_the_widened_scan(path: str, fixture: str, fmt: str) -> None:
    # given — the scan runs over the projection, so a credential the preset cuts never reaches it
    policy = BuiltinCatalog.default().policy_for(path)
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    projected = text if policy is None else project_text(JsoncAdapter(), text, policy)

    # when
    result = PatternSecretScanner().scan(projected.encode("utf-8"), subject=path, format=fmt)

    # then
    assert result is None


# Fixtures the presets above do not project, scanned as they are: the AWS secret key rule (#63) is a new
# key-name pattern in every format, so each sample format of the repository has to still pass it.
RAW_CASES = (
    ("ghostty/config", "text"),
    ("orca/settings.json", "jsonc"),
    ("zed/settings.jsonc", "jsonc"),
    ("zed/keymap.json", "jsonc"),
    ("vscode/storage.json", "jsonc"),
    ("iterm2/dynamic-profiles.json", "jsonc"),
    ("plist/prefs.plist", "plist"),
    ("xml/options.xml", "xml"),
    ("jetbrains/ide.general.xml", "xml"),
    ("jetbrains/databaseSettings.xml", "xml"),
)


@pytest.mark.parametrize(("fixture", "fmt"), RAW_CASES)
def test_a_raw_fixture_passes_the_aws_secret_key_rule(fixture: str, fmt: str) -> None:
    # given
    data = (FIXTURES / fixture).read_bytes()

    # when
    result = PatternSecretScanner().scan(data, subject=fixture, format=fmt)

    # then
    assert result is None
