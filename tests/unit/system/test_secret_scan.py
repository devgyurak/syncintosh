# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the secret scan that gates the encrypt boundary.

The patterns are written by concatenation so this file never holds a value that a separate
secret scanner would flag, and the assertions check that the error names the **location** only
(``AGENTS.md`` §4).

The last two groups are about URLs, and a URL is read by the *format* of the file it sits in: a shell
file may reference a credential, a JSON settings file has no interpolation, so the same bytes mean
different things. Every case names its format.
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
        scanner.scan(payload, subject=".zshrc", format="text")
    assert label in str(caught.value)
    assert ".zshrc" in str(caught.value)


def test_clean_text_passes() -> None:
    # given
    scanner = PatternSecretScanner()

    # when
    result = scanner.scan(b"export EDITOR=vim\nset -o vi\n", subject=".zshrc", format="text")

    # then
    assert result is None


def test_the_error_names_the_line_and_never_the_value() -> None:
    # given — the secret is on the third line
    scanner = PatternSecretScanner()
    payload = b"first\nsecond\n" + GITHUB + b"\nfourth\n"

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject=".zshrc", format="text")
    assert "line 3" in str(caught.value)
    assert GITHUB.decode() not in str(caught.value)


def test_the_first_match_is_reported() -> None:
    # given — the same pattern twice, on the first and third lines
    scanner = PatternSecretScanner()
    payload = GITHUB + b"\nsecond\n" + GITHUB + b"\n"

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject="settings.json", format="jsonc")
    assert "line 1" in str(caught.value)


# --------------------------------------------------------------------------- a structured file


@pytest.mark.parametrize(
    "payload",
    [
        b'{"http.proxy": "http://user:$ecretPass@proxy"}',  # `$` is an ordinary character in JSON
        b'{"http.proxy": "http://user:pa$$w0rd@proxy"}',
        b'{"http.proxy": "http://user:abc{123}@proxy"}',
        b'{"http.proxy": "http://user:pass$word@proxy"}',
        b'{"http.proxy": "http://:s3cretPass@proxy"}',  # no user: the password is the secret
        b'{"http.proxy": "http://user:pa\\\\ss@proxy"}',  # a backslash value, written `\\\\` in JSON
    ],
)
def test_a_literal_password_in_a_structured_file_is_refused(payload: bytes) -> None:
    # given — JSON has no interpolation, so `$` and `{` are part of the value like any other byte
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject="settings.json", format="jsonc")
    assert "URL with credentials" in str(caught.value)


# --------------------------------------------------------------------------- a shell file


@pytest.mark.parametrize(
    "payload",
    [
        b"export http_proxy=http://$USER:$PASS@proxy.test:3128",
        b'export http_proxy="http://$USER:$PASS@proxy.test:3128"',  # double-quoted: still expanded
        b"postgres://app:${PGPASSWORD}@localhost/db",
        b"https://oauth2:${TOKEN}@gitlab.example.test/repo.git",
    ],
)
def test_a_reference_in_a_shell_file_passes(payload: bytes) -> None:
    # given — a reference is the recommended way to keep a secret out of a dotfile, and a variable
    # name is not a value; refusing it would block the file precisely when its owner did this right
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject=".zshrc", format="text") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"proxy='http://user:$ecretPass@host'",  # single quotes: the shell passes it through
        b"proxy=http://user:\\$ecretPass@host",  # escaped: likewise
        b"postgres://app:pass$word@db",
        b"http://user:abc{123}@host",
        b"proxy='http://user:pa$$w0rd@host'",
        b"proxy='http://user:pa,ss@host'",  # inside quotes, `,` is part of the value
        b"proxy='http://user:pa?ss@host'",
        b"proxy='http://user:pa#ss@host'",
        b"proxy='http://user:pa\"ss@host'",
    ],
)
def test_a_literal_password_in_a_shell_file_is_refused(payload: bytes) -> None:
    # given — the quoting says the shell will not expand it, so the text itself is the credential
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject=".zshrc", format="text")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"proxy":"http://h:3128","email":"a@b.c"}',  # a port and a later `@` are not a credential
        b'{"a": "https://host:8443/path"}',
        b"NO_PROXY=http://h:80,user@x.example.test",
        b"ssh://git@github.com/user/repo.git",
        b"git@github.com:user/repo.git",
        b"redis://cache.example.test:6379/0",
        b"https://example.test/path?token=looks-secret",
        b'{"http.proxy": "http://u:pa?ss@h"}',  # `?` starts a query, so `pa` is the password
        b'{"http.proxy": "http://u:pa#ss@h"}',  # `#` starts a fragment
    ],
)
def test_a_line_that_merely_carries_an_at_sign_passes(payload: bytes) -> None:
    # given — a false positive here stops every tracked file from syncing, so the pattern only
    # matches a password-shaped span between a `:` and an `@` on one line
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject="settings.json", format="jsonc") is None
    assert scanner.scan(payload, subject=".zshrc", format="text") is None


def test_a_url_split_across_a_line_continuation_passes() -> None:
    # given — the one shape the pattern cannot reach: `\s` is excluded so a match never spans a line,
    # and a byte pattern cannot join a shell continuation's two halves. Pinned so the limit is a
    # decision rather than an accident; the value is in the file either way, which is what the other
    # patterns and the user's own review are for.
    scanner = PatternSecretScanner()
    payload = b"http://user:pa\\" + b"\n" + b"ssw0rd-placeholder@host"

    # when & then
    assert scanner.scan(payload, subject=".zshrc", format="text") is None


def test_a_quoted_url_followed_by_an_address_is_not_a_credential() -> None:
    # given — a shell line where a quoted URL is followed by an email address; the `;` and the quote
    # are what separates them, not part of a password
    scanner = PatternSecretScanner()
    payload = b"m='http://h:3128';me@x.com"

    # when & then
    assert scanner.scan(payload, subject=".zshrc", format="text") is None


def test_a_semicolon_in_a_password_fails_closed() -> None:
    # given — RFC 3986 allows `;` in a userinfo, and the same bytes appear between two values in a
    # `;`-separated list; the two cannot be told apart from the text alone
    scanner = PatternSecretScanner()

    # when & then — it fails **closed**, because the shapes are ambiguous: a refused sync is visible
    # and names its reason, a leaked password is neither. Issue #25 narrows this with a user-defined
    # allowlist rather than by loosening the pattern.
    with pytest.raises(SecretScanError):
        scanner.scan(b"http://me:s3cr;t@proxy:8080", subject="settings.json", format="jsonc")
    with pytest.raises(SecretScanError):
        scanner.scan(b'export http_proxy="http://me:s3cr;t@proxy:8080"', subject=".zshrc", format="text")
