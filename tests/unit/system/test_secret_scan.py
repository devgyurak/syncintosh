# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the secret scan that gates the encrypt boundary.

The patterns are written by concatenation so this file never holds a value that a separate
secret scanner would flag, and the assertions check that the error names the **location** only
(``AGENTS.md`` §4).
"""

from __future__ import annotations

import pytest

from syncintosh.core.errors import SecretScanError
from syncintosh.system.secret_scan import PatternSecretScanner

# Built from pieces so the source file does not itself contain a token-shaped literal.
PEM = b"-----BEGIN " + b"RSA PRIVATE KEY-----"
AWS = b"AKIA" + b"ABCDEFGHIJKLMNOP"
GITHUB = b"ghp_" + b"a" * 36
GITHUB_PAT = b"github_pat_" + b"b" * 24
SLACK = b"xoxb-" + b"0" * 12

SECRETS = [
    ("private key", PEM),
    ("AWS access key id", AWS),
    ("GitHub token", GITHUB),
    ("GitHub fine-grained token", GITHUB_PAT),
    ("Slack token", SLACK),
]


@pytest.mark.parametrize(("label", "payload"), SECRETS)
def test_each_pattern_is_detected(label: str, payload: bytes) -> None:
    # given
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject=".zshrc")
    assert label in str(caught.value)
    assert ".zshrc" in str(caught.value)


def test_clean_text_passes() -> None:
    # given
    scanner = PatternSecretScanner()

    # when
    result = scanner.scan(b"export EDITOR=vim\nset -o vi\n", subject=".zshrc")

    # then
    assert result is None


def test_the_error_names_the_line_and_never_the_value() -> None:
    # given — the secret is on the third line
    scanner = PatternSecretScanner()
    payload = b"first\nsecond\n" + GITHUB + b"\nfourth\n"

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject=".zshrc")
    assert "line 3" in str(caught.value)
    assert GITHUB.decode() not in str(caught.value)


def test_the_first_match_is_reported() -> None:
    # given — the same pattern twice, on the first and third lines
    scanner = PatternSecretScanner()
    payload = GITHUB + b"\nsecond\n" + GITHUB + b"\n"

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject="settings.json")
    assert "line 1" in str(caught.value)
