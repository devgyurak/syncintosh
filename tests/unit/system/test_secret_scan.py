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
# A proxy URL with inline credentials — the shape an `http.proxy` setting or a `.zshrc` token
# URL takes. Concatenated for the same reason as the others.
PROXY_URL = b"http://" + b"builder" + b":" + b"pw-placeholder" + b"@" + b"proxy.example.test:3128"
# JSON allows the slashes to be escaped (`\/`), and the projection preserves an unchanged
# value verbatim, so the escaped spelling has to be matched too.
ESCAPED_PROXY_URL = rb"http:\/\/" + b"builder" + b":" + b"pw-placeholder" + b"@" + b"proxy.example.test:3128"

SECRETS = [
    ("private key", PEM),
    ("AWS access key id", AWS),
    ("GitHub token", GITHUB),
    ("GitHub fine-grained token", GITHUB_PAT),
    ("Slack token", SLACK),
    ("URL with credentials", PROXY_URL),
    ("URL with credentials", ESCAPED_PROXY_URL),
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


@pytest.mark.parametrize(
    "payload",
    [
        b"http://proxy.example.test:3128",  # a host and a port, no userinfo
        b"ssh://git@github.com/user/repo.git",  # a user, no password
        b"https://example.test/path?token=looks-secret",  # a query parameter, not userinfo
        b"redis://cache.example.test:6379/0",
        b"https://:8080/path",  # an empty user and a port are not a credential
    ],
)
def test_a_url_without_inline_credentials_passes(payload: bytes) -> None:
    # given — the `@` and the port alone are not a credential (decision V2: high signal only)
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject="settings.json") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"postgres://app:$DB_PASS@localhost/db",  # a variable reference, not a value
        b"postgres://app:${PGPASSWORD}@localhost/db",
        b'http_proxy="http://$USER:$PASS@proxy.example.test:3128"',
    ],
)
def test_a_url_that_only_references_its_credentials_passes(payload: bytes) -> None:
    # given — a URL interpolating a variable is the recommended way to keep a secret out of the
    # file, so refusing it would block the file for doing the right thing
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject=".zshrc") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"http://user:pa$$w0rd@proxy.example.test",  # a `$` inside a literal password
        b"https://user:abc{123}@host",  # and a `{` — both are ordinary characters in a value
        b"'postgres://app:pass$word@db'",
        b"postgres://app:s3cret-placeholder@db.internal:5432",
        b"http:\\/\\/user:pa$$w0rd@proxy.example.test",  # the JSON-escaped spelling too
        b"http://:s3cret-placeholder@host",  # no user, so the password is the secret
        b'{"http.proxy": "http://user:pa\\\\ss-placeholder@host"}',  # a backslash in the value
        b"postgres://app:${PASS:-fallback}@db",  # a default may itself hold the secret: fail closed
        b"http://user:$1@proxy.example.test",  # a positional parameter is not the recommended form
        b'http://user:"$PASS"@proxy.example.test',
    ],
)
def test_a_literal_password_in_a_url_is_refused(payload: bytes) -> None:
    # given — the whole point of the pattern is a value in the file, and skipping a *reference* must
    # not skip anything else: a password containing `$` or `{` is still a credential leaving the
    # machine (the review found this regression in an earlier revision of the pattern)
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject=".zshrc")


def test_a_url_split_across_a_line_continuation_passes() -> None:
    # given — the one shape the pattern cannot reach: `\s` is excluded so a match never spans a
    # line, and a byte pattern cannot join a shell line continuation's two halves. Pinned so the
    # limit is a decision rather than an accident; the value is still in the file either way, which
    # is what the dragnet's other patterns and the user's own review are for.
    scanner = PatternSecretScanner()
    payload = b"http://user:pa\\" + b"\n" + b"ssw0rd-placeholder@host"

    # when & then
    assert scanner.scan(payload, subject=".zshrc") is None
