# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for the local scan allowlist (#25).

The allowlist exists so a real refused span can be unblocked **on this machine and nowhere else**,
so this drives the CLI and the on-disk allowlist together against a real file, in the temp HOME the
suite sets up (``AGENTS.md`` §5.6). No network, no real keychain.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from syncintosh.cli import main as main_module
from syncintosh.core.errors import SecretScanError
from syncintosh.system.paths import MachinePaths
from syncintosh.system.scan_allow import load_scan_allow
from syncintosh.system.secret_scan import PatternSecretScanner

pytestmark = pytest.mark.integration

runner = CliRunner()

# The `;`-in-a-password URL the scan fails closed on (#25), and a second, different one.
SPAN = b"http://me:s3cr;t@proxy:8080"
OTHER = b"http://me:pa,ss@proxy:9090"


def test_a_refused_span_can_be_allowed_on_this_machine_only() -> None:
    # given — the scan refuses the ambiguous span and prints a fingerprint, exactly as a real cycle
    allow_file = MachinePaths.default().scan_allow_file
    before = PatternSecretScanner(load_scan_allow(allow_file))
    with pytest.raises(SecretScanError) as caught:
        before.scan(SPAN, subject="settings.json", format="jsonc")
    digest = str(caught.value).split("fingerprint ")[1].split(";")[0]

    # when — the user records the fingerprint the refusal printed
    result = runner.invoke(main_module.app, ["scan", "allow", "settings.json", "URL with credentials", digest])

    # then — the allowed span passes, a different one is still refused, and the cached file reloads
    after = PatternSecretScanner(load_scan_allow(allow_file))
    assert result.exit_code == 0
    assert after.scan(SPAN, subject="settings.json", format="jsonc") is None
    with pytest.raises(SecretScanError):
        after.scan(OTHER, subject="settings.json", format="jsonc")
