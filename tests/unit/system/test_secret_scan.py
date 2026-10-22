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

import time

import pytest

from syncintosh.core.errors import SecretScanError
from syncintosh.system.scan_allow import ALLOWABLE_LABELS, ScanAllow, ScanAllowEntry
from syncintosh.system.secret_scan import SHELL_PATTERNS, STRUCTURED_PATTERNS, PatternSecretScanner

# Built from pieces so the source file does not itself contain a token-shaped literal.
PEM = b"-----BEGIN " + b"RSA PRIVATE KEY-----"
AWS = b"AKIA" + b"ABCDEFGHIJKLMNOP"
GITHUB = b"ghp_" + b"a" * 36
GITHUB_PAT = b"github_pat_" + b"b" * 24
SLACK = b"xoxb-" + b"0" * 12

# A fixed key for the allowlist tests, so a fingerprint is deterministic here.
ALLOW_KEY = b"k" * 32

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
        b'{"http.proxy": "http://user:pa,ss@proxy"}',  # `,` is legal in RFC 3986's userinfo too
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
        b"proxy='http://user:pa;ss@host'",  # and so is `;` — the quote is what ends it, not a `;`
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
    with pytest.raises(SecretScanError):
        # single quotes pass the URL through verbatim, so the `;` is part of the password here too;
        # only the quote ends it, which is why `'` is the one character the quoted rule excludes
        scanner.scan(b"export http_proxy='http://me:s3cr;t@proxy:8080'", subject=".zshrc", format="text")


def test_a_comma_in_a_password_fails_closed() -> None:
    # given — `,` is legal in RFC 3986's userinfo, and it is also what separates two entries in a
    # `NO_PROXY` list, so the same bytes are a credential to one reader and a list to the other
    scanner = PatternSecretScanner()

    # when & then — fail closed, for the same reason as `;`. The round-4 false positive
    # (`{"proxy":"http://h:3128","email":"a@b.c"}`) stays fixed: there the `"` that ends the JSON
    # string is what stops the match, and a `, ` inside a value is still stopped by the space.
    with pytest.raises(SecretScanError):
        scanner.scan(b'{"http.proxy": "http://me:pa,ss@proxy:8080"}', subject="settings.json", format="jsonc")
    with pytest.raises(SecretScanError):
        scanner.scan(b"NO_PROXY=http://h:80,user@x.example.test", subject=".zshrc", format="text")
    assert scanner.scan(b'{"proxy":"http://h:3128","email":"a@b.c"}', subject="s.json", format="jsonc") is None
    assert scanner.scan(b'{"a":"http://h:3128", "b":"x@y"}', subject="s.json", format="jsonc") is None


def test_a_credential_left_in_a_comment_is_refused() -> None:
    # given — the VS Code preset excludes `http.proxyAuthorization` by *key*, so a copy of the
    # credential that is not under that key — commented out, or under a name this build has never
    # heard of — is not excluded at all. `Basic <base64>` is what such a copy looks like.
    scanner = PatternSecretScanner()
    payload = b'{\n  // "http.proxyAuthorization": "Basic dXNlcjpwYXNzd29yZA=="\n}'

    # when & then — the scan is the backstop for exactly this, so it refuses it
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject="settings.json", format="jsonc")
    assert "Basic" in str(caught.value)


def test_a_comment_about_basic_auth_is_not_a_credential() -> None:
    # given — a comment a person writes, where the word after `Basic` is prose and not a token: a
    # refusal here would stop every tracked file on the machine for nothing
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(b"// Basic authentication is configured below", subject="s.json", format="jsonc") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"// Basic internationalization settings",
        b"// Basic settings/preferences/overrides",
        b"// Visual Basic\n" + b"/" * 32,  # a banner on the next line, which `\s+` used to join to it
        b"// Basic " + b"/" * 32,  # and a banner on the same line: a run of slashes is not a token
    ],
)
def test_a_comment_that_only_says_basic_is_not_a_credential(payload: bytes) -> None:
    # given — comments a person writes in a settings file. The word after `Basic` is prose, and a run
    # of `/` is a banner; neither is base64, and refusing one stops every tracked file on the machine
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject="settings.json", format="jsonc") is None


def test_an_escaped_quote_in_a_password_is_refused() -> None:
    # given — JSONC writes a quote inside a value as `\"`, which is one character of the password
    scanner = PatternSecretScanner()
    payload = b'{"http.proxy": "http://me:pa\\"ss@proxy:8080"}'

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject="settings.json", format="jsonc")


# --------------------------------------------------------------------------- the local allowlist

# The ambiguous span #25 exists for: a `;` in a URL's userinfo is legal RFC 3986 and the same bytes
# separate list entries, so the scan fails closed and the user needs a way to allow that one line.
AMBIGUOUS_URL = b"http://me:s3cr;t@proxy:8080"


def _fingerprint_of(payload: bytes, key: bytes) -> str:
    """The fingerprint a refusal prints for `payload`, read back out of the message."""
    scanner = PatternSecretScanner(ScanAllow(key=key, entries=frozenset()))
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject="settings.json", format="jsonc")
    return str(caught.value).split("fingerprint ")[1].split(";")[0].strip()


def test_a_refusal_names_the_fingerprint_and_the_way_to_allow_it() -> None:
    # given — an allowlistable shape, so the refusal offers the command to record it
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset()))

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(AMBIGUOUS_URL, subject="settings.json", format="jsonc")
    assert "URL with credentials" in str(caught.value)
    assert "scan allow" in str(caught.value)
    assert AMBIGUOUS_URL.decode() not in str(caught.value)


def test_a_refusal_for_an_unallowable_shape_does_not_offer_to_allow_it() -> None:
    # given — a private key header is fixed text: one allowed entry would pass every key it stands in
    # for, so the label is not allowlistable and the message must not suggest recording it
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset()))

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(PEM, subject=".zshrc", format="text")
    assert "private key" in str(caught.value)
    assert "scan allow" not in str(caught.value)


def test_an_allowed_span_passes() -> None:
    # given
    digest = _fingerprint_of(AMBIGUOUS_URL, ALLOW_KEY)
    entry = ScanAllowEntry("settings.json", "URL with credentials", digest)
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # when & then
    assert scanner.scan(AMBIGUOUS_URL, subject="settings.json", format="jsonc") is None


def test_an_allowlist_entry_is_scoped_to_its_path() -> None:
    # given — allowing a span in one file must not allow the same bytes everywhere
    digest = _fingerprint_of(AMBIGUOUS_URL, ALLOW_KEY)
    entry = ScanAllowEntry("settings.json", "URL with credentials", digest)
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(AMBIGUOUS_URL, subject=".zshrc", format="jsonc")


def test_an_entry_for_another_span_does_not_allow_this_one() -> None:
    # given — a fingerprint is over one exact span, so a different span's print does not match
    other = _fingerprint_of(b"http://me:pa,ss@proxy:9090", ALLOW_KEY)
    entry = ScanAllowEntry("settings.json", "URL with credentials", other)
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(AMBIGUOUS_URL, subject="settings.json", format="jsonc")


def test_an_allowed_span_does_not_hide_a_later_one() -> None:
    # given — `finditer` visits every match, so allowing the first URL still refuses the second
    first = b"http://me:s3cr;t@proxy:8080"
    second = b"http://me:pa,ss@proxy:9090"
    entry = ScanAllowEntry("settings.json", "URL with credentials", _fingerprint_of(first, ALLOW_KEY))
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(first + b"\n" + second, subject="settings.json", format="jsonc")


def test_without_an_allowlist_a_refusal_carries_no_fingerprint() -> None:
    # given — a build with no key (or a unit test) still refuses, just without the fingerprint
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(AMBIGUOUS_URL, subject="settings.json", format="jsonc")
    assert "fingerprint" not in str(caught.value)


def test_every_allowable_label_is_a_real_pattern_label() -> None:
    # when — the allowlist's policy names labels, and the labels live in the pattern tables
    labels = {label for label, _pattern in STRUCTURED_PATTERNS} | {label for label, _pattern in SHELL_PATTERNS}

    # then — a renamed pattern would otherwise leave the allowlist unable to name what it allows
    assert ALLOWABLE_LABELS - labels == set()


# --------------------------------------------------------------------------- vendor prefixes (#28)

# Built by concatenation for the same reason as the block near the top: the source never holds a
# token-shaped literal, so a push-protection or push hook does not trip on this file.
VENDOR_TOKENS = [
    ("OpenAI API key", b"sk-proj-" + b"A" * 40),
    ("OpenAI legacy API key", b"sk-" + b"A" * 20 + b"T3BlbkFJ" + b"B" * 20),
    ("Anthropic API key", b"sk-ant-api03-" + b"A" * 40),
    ("GitLab token", b"glpat-" + b"a" * 20),
    ("npm token", b"npm_" + b"a" * 36),
    ("PyPI token", b"pypi-AgEIcHlwaS5vcmc" + b"a" * 20),
    ("Docker Hub token", b"dckr_pat_" + b"a" * 27),
    ("Hugging Face token", b"hf_" + b"a" * 34),
    ("Google API key", b"AIza" + b"a" * 35),
    ("Stripe key", b"sk_live_" + b"a" * 24),
]


@pytest.mark.parametrize(("label", "token"), VENDOR_TOKENS)
def test_each_fixed_vendor_prefix_is_detected(label: str, token: bytes) -> None:
    # given — a token whose prefix the vendor documents
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(b"value = " + token, subject="settings.json", format="jsonc")
    assert label in str(caught.value)
    assert token.decode() not in str(caught.value)


@pytest.mark.parametrize("scheme", [b"Basic", b"basic", b"BASIC"])
def test_the_basic_scheme_is_case_insensitive(scheme: bytes) -> None:
    # given — HTTP scheme names are case-insensitive, so a file may spell any of these
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(scheme + b" " + b"dXNlcjpwYXNzd29yZA==", subject="settings.json", format="jsonc")


def test_a_bearer_token_is_detected() -> None:
    # given — the header in common use today, which the Basic-only rule missed
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(b"Bearer " + b"eyJhbGciOiJIUzI1NiJ9", subject="settings.json", format="jsonc")


# --------------------------------------------------------------------------- the Authorization key

AUTHORIZATION_STRUCTURED_REFUSED = [
    b'{"Authorization": "Bearer xyz"}',
    b'{"Proxy-Authorization": "Basic abcd"}',
    b'{"Authorization": "basic dXNlcjpwYXNzd29yZA=="}',
    b'{"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9"}',
    b'{"Authorization": "X-Unknown abcdef0123456789"}',  # an unlisted scheme, encoded body
    b"<key>Authorization</key>\n<string>Bearer xyz</string>",  # plist / XML
]


@pytest.mark.parametrize("payload", AUTHORIZATION_STRUCTURED_REFUSED)
def test_a_credential_shaped_structured_value_under_an_authorization_key_is_refused(payload: bytes) -> None:
    # given — a known scheme with a token, an encoded body, or a plist/XML header; whichever rule
    # fires, the credential is refused
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject="settings.json", format="jsonc")


def test_the_authorization_rule_catches_a_shape_the_scheme_rules_miss() -> None:
    # given — an unlisted scheme with an encoded body: the `Basic`/`Bearer` rules do not match it
    scanner = PatternSecretScanner()

    # when & then — the key rule does, and names itself
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(b'{"Authorization": "X-Unknown abcdef0123456789"}', subject="settings.json", format="jsonc")
    assert "authorization header" in str(caught.value)


AUTHORIZATION_STRUCTURED_ALLOWED = [
    b'{"Authorization": ""}',
    b'{"Authorization": "null"}',
    b'{"Authorization": "true"}',
    b'{"Authorization": "${input:token}"}',  # a VS Code placeholder
    b'{"Authorization": "${env:TOKEN}"}',
    b'{"Authorization": "see the runbook"}',  # prose, not a credential
    b"// Authorization: see the runbook",  # a comment about it
]


@pytest.mark.parametrize("payload", AUTHORIZATION_STRUCTURED_ALLOWED)
def test_a_harmless_structured_value_under_an_authorization_key_passes(payload: bytes) -> None:
    # given — a refusal here stops every tracked file on the machine (§6), so placeholders, JSON
    # literals, and prose must all pass
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject="settings.json", format="jsonc") is None


AUTHORIZATION_SHELL_REFUSED = [
    b"Authorization: Basic abcd",
    b'-H "Authorization: Bearer TOKEN-abc"',
    b'Authorization = "Bearer token123"',
    b"Proxy-Authorization: Bearer x",
    b"curl -H 'Proxy-Authorization: Basic cd'",
    b"Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
]


@pytest.mark.parametrize("payload", AUTHORIZATION_SHELL_REFUSED)
def test_a_credential_shaped_shell_value_under_an_authorization_key_is_refused(payload: bytes) -> None:
    # given — a header written into a dotfile or passed to `curl -H`
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject=".zshrc", format="text")


AUTHORIZATION_SHELL_ALLOWED = [
    b"Authorization: Bearer $TOKEN",
    b"Authorization: ${AUTH}",
    b'-H "Authorization: Basic ${BASIC_AUTH}"',
    b"Proxy-Authorization: $PROXY_AUTH",
    b"Authorization:",
    b"Authorization: '$TOKEN'",  # the file holds a broken header, not a secret
    b"Authorization: 'Bearer $TOKEN'",
    # the shape the earlier rule wrongly refused: a reference header followed by the URL
    b'curl -H "Authorization: Bearer $TOKEN" https://api.example.test/v1',
    b"# Authorization: see the runbook",
    b"# Authorization: don't hard-code one",
]


@pytest.mark.parametrize("payload", AUTHORIZATION_SHELL_ALLOWED)
def test_a_harmless_shell_value_under_an_authorization_key_passes(payload: bytes) -> None:
    # given — a reference is the recommended way to keep a credential out of a dotfile, and prose is
    # not a credential; refusing either would stop the whole file for nothing
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(payload, subject=".zshrc", format="text") is None


def test_a_bearer_token_whose_digit_is_not_in_the_first_characters_is_caught() -> None:
    # given — a single token long enough that a 16-character window would miss its digit
    scanner = PatternSecretScanner()
    payload = b"Value = Bearer abcdefghijklmnopqrstuv0123456789"

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject=".zshrc", format="text")


def test_a_long_reference_header_scans_without_backtracking() -> None:
    # given — the shape the previous `(?:…|…)*` skip form backtracked on exponentially; this returns
    # promptly only because the rule is a flat character class now
    scanner = PatternSecretScanner()
    payload = b"Authorization: Bearer " + b"$TOKEN " * 200

    # when
    result = scanner.scan(payload, subject=".zshrc", format="text")

    # then
    assert result is None


# Shapes the widened rules must **not** refuse: a bare word, a mustache placeholder, a command
# substitution, backticks, and a dropped-quoting concat are all "the value lives elsewhere".
AUTHORIZATION_MORE_ALLOWED = [
    (b'{"Authorization": "abcdefghijklmnopqrst"}', "jsonc"),
    (b'{"Authorization": "PleaseSeeTheRunbook"}', "jsonc"),
    (b'{"Authorization": "Bearer {{token}}"}', "jsonc"),
    (b"Authorization: Bearer $(op read op://vault/token)", "text"),
    (b"Authorization: Bearer `cat ~/.tok`", "text"),
    (b"'Authorization: Bearer '\"$TOKEN\"", "text"),
    (b"Authorization: Bearer {{$dotenv API_TOKEN}}", "text"),
]


@pytest.mark.parametrize(("payload", "fmt"), AUTHORIZATION_MORE_ALLOWED)
def test_a_value_that_points_elsewhere_is_not_a_credential(payload: bytes, fmt: str) -> None:
    # given — none of these holds the credential itself
    scanner = PatternSecretScanner()

    # when & then
    subject = "settings.json" if fmt == "jsonc" else ".zshrc"
    assert scanner.scan(payload, subject=subject, format=fmt) is None


def test_an_xml_attribute_bearer_token_is_refused() -> None:
    # given — a token with dots, which the old character class stopped at
    scanner = PatternSecretScanner()
    payload = b'<string value="Bearer ya29.a0AfH6AbCdEf1234567890"/>'

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject="x.plist", format="plist")


def test_a_long_run_of_spaces_after_the_key_is_not_backtracked() -> None:
    # given — the shape that took seconds with a backtracking separator
    scanner = PatternSecretScanner()
    payload = b'{"Authorization": ' + b" " * 30000 + b'"x"}'

    # when
    result = scanner.scan(payload, subject="settings.json", format="jsonc")

    # then — it returns promptly; a backtracking `[ \t]*` would take seconds
    assert result is None


def test_a_false_positive_from_the_new_rules_can_be_allowed() -> None:
    # given — a line the `Authorization` rule refuses
    payload = b"Authorization: Basic abcd"
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset()))
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(payload, subject=".zshrc", format="text")

    # when — the refusal offers the command and its label is allowlistable
    assert "scan allow" in str(caught.value)
    digest = str(caught.value).split("fingerprint ")[1].split(";")[0].strip()
    entry = ScanAllowEntry(".zshrc", "authorization header", digest)
    relaxed = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # then — that one line passes, and a different value is still refused
    assert relaxed.scan(payload, subject=".zshrc", format="text") is None
    with pytest.raises(SecretScanError):
        relaxed.scan(b"Authorization: Bearer abc123", subject=".zshrc", format="text")


def test_allowing_one_long_header_does_not_allow_another() -> None:
    # given — two OAuth headers that share the span a pattern matches (it stops at the `,`) and differ
    # only in the signature after it
    first = b'{"Authorization": "OAuth oauth_consumer_key=abc, oauth_signature=SIG1"}'
    second = b'{"Authorization": "OAuth oauth_consumer_key=abc, oauth_signature=SIG2"}'
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset()))
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(first, subject="s.json", format="jsonc")

    # when — the fingerprint is over the whole line, and that line is allowed
    digest = str(caught.value).split("fingerprint ")[1].split(";")[0].strip()
    entry = ScanAllowEntry("s.json", "authorization header", digest)
    relaxed = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # then — the allowed line passes and the other signature is still refused
    assert relaxed.scan(first, subject="s.json", format="jsonc") is None
    with pytest.raises(SecretScanError):
        relaxed.scan(second, subject="s.json", format="jsonc")


def test_a_bare_url_under_an_authorization_key_is_not_a_credential() -> None:
    # given — a YAML value that is a URL, where a scheme follows `:` without being a header scheme
    scanner = PatternSecretScanner()

    # when & then
    assert (
        scanner.scan(b"Authorization: https://login.example.com/oauth/authorize", subject="a.yaml", format="yaml")
        is None
    )


AUTHORIZATION_LATE_ROUND = [
    (b'{"Authorization": "Bearer {{x}} <token>"}', "jsonc"),
    (b"Authorization: Bearer {{x}} <token>", "text"),
    (b"Authorization: Token abc123", "text"),
    (b"<key>Authorization</key>\n<string>Token abc123</string>", "plist"),
]


@pytest.mark.parametrize(("payload", "fmt"), AUTHORIZATION_LATE_ROUND)
def test_a_token_behind_a_placeholder_or_under_the_token_scheme_is_refused(payload: bytes, fmt: str) -> None:
    # given — a placeholder must not hide the token behind it, and `Token` is a header scheme
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject="s.json" if fmt == "jsonc" else "a.txt", format=fmt)


def test_a_hundred_thousand_letters_are_scanned_in_linear_time() -> None:
    # given — a value with no digit at all, the shape an unbounded `[…]*[0-9=]` lookahead made quadratic
    scanner = PatternSecretScanner()
    payload = b"Authorization: " + b"a" * 100_000

    # when
    result = scanner.scan(payload, subject=".zshrc", format="text")

    # then — it returns promptly
    assert result is None


def test_many_authorization_keys_on_one_line_scan_in_linear_time() -> None:
    # given — the key repeated without a newline, so each match would re-scan the rest of the line
    scanner = PatternSecretScanner()
    payload = b"Authorization:" * 10_000

    # when
    result = scanner.scan(payload, subject=".zshrc", format="text")

    # then — it returns promptly
    assert result is None


def test_a_bare_grant_name_and_a_camelcase_word_are_not_credentials() -> None:
    # given — the shapes the "both letter cases" and bare-token tests used to refuse
    scanner = PatternSecretScanner()

    # when & then
    assert scanner.scan(b"Authorization: oauth2-client-credentials", subject="a.yaml", format="yaml") is None
    assert scanner.scan(b"// Basic ConfigurationOverridesForEditor", subject="s.json", format="jsonc") is None


def test_allowing_one_plist_header_does_not_allow_another() -> None:
    # given — two plist headers whose `<key>` line is identical and only the `<string>` line differs
    first = b"<key>Authorization</key>\n<string>Bearer SIG1</string>"
    second = b"<key>Authorization</key>\n<string>Bearer SIG2</string>"
    scanner = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset()))
    with pytest.raises(SecretScanError) as caught:
        scanner.scan(first, subject="a.plist", format="plist")

    # when — the fingerprint covers every line the match spans, not only its first
    digest = str(caught.value).split("fingerprint ")[1].split(";")[0].strip()
    entry = ScanAllowEntry("a.plist", "authorization header", digest)
    relaxed = PatternSecretScanner(ScanAllow(key=ALLOW_KEY, entries=frozenset({entry})))

    # then
    assert relaxed.scan(first, subject="a.plist", format="plist") is None
    with pytest.raises(SecretScanError):
        relaxed.scan(second, subject="a.plist", format="plist")


REDOS_INPUTS = [
    (b"Authorization: " + b"a" * 100_000, "text"),
    (b'{"Authorization": "' + b"a" * 100_000 + b'"}', "jsonc"),
    (b"Authorization:" * 10_000, "text"),
    (b'{"Authorization":' * 10_000, "jsonc"),
    (b"http://" + b"a" * 100_000, "jsonc"),
    # an unclosed placeholder: `Bearer ${x ${x ${x …` is a harmless placeholder run, and the filler
    # is **bounded** (`[^}]{0,256}`) so it is scanned once. Unbounded, the `(?:gap filler)*` repetition
    # backtracked through the whole run at every split — 0.2 s at 24 KB, ~20 s at this size.
    (b"Authorization: Bearer " + b"${x " * 60_000, "text"),
]


# The unclosed runs that read as a **token** rather than a placeholder: `{{x`, `$(x`, and `` `x ``
# never reach a closing delimiter, so the scheme's value is refused. Same bound, same linearity.
REDOS_PLACEHOLDER_INPUTS = [
    (b'{"Authorization": "Bearer ' + b"{{x " * 60_000 + b'"}', "jsonc"),
    (b"Authorization: Bearer " + b"$(x " * 60_000, "text"),
    (b"Authorization: Bearer " + b"`x " * 60_000, "text"),
]

# The rules scan 100 KB in tens of milliseconds; the quadratic spellings of the same input took
# seconds. Two orders of magnitude of headroom keeps this from flaking on a loaded runner while
# still failing on the shape that prompted it — a bound in time is the only thing that makes a
# "linear time" claim testable, since a slow scan is otherwise a passing test.
LINEAR_SCAN_SECONDS = 5.0


@pytest.mark.parametrize(("payload", "fmt"), REDOS_INPUTS)
def test_a_pathological_line_is_scanned_in_linear_time(payload: bytes, fmt: str) -> None:
    # given — the inputs that took seconds while a lookahead scanned to the end of the line
    scanner = PatternSecretScanner()

    # when
    started = time.monotonic()
    result = scanner.scan(payload, subject=".zshrc", format=fmt)
    elapsed = time.monotonic() - started

    # then — it returns promptly, and is not a secret
    assert result is None
    assert elapsed < LINEAR_SCAN_SECONDS


@pytest.mark.parametrize(("payload", "fmt"), REDOS_PLACEHOLDER_INPUTS)
def test_a_pathological_placeholder_run_is_scanned_in_linear_time(payload: bytes, fmt: str) -> None:
    # given
    scanner = PatternSecretScanner()

    # when
    started = time.monotonic()
    with pytest.raises(SecretScanError):
        scanner.scan(payload, subject=".zshrc", format=fmt)
    elapsed = time.monotonic() - started

    # then
    assert elapsed < LINEAR_SCAN_SECONDS


def test_a_json_escaped_whitespace_between_a_scheme_and_its_token_is_refused() -> None:
    # given — a value a structured file actually holds: the whitespace is written `\t`
    scanner = PatternSecretScanner()

    # when & then
    with pytest.raises(SecretScanError):
        scanner.scan(b'{"Authorization": "Bearer\\tTOKEN123456789012345"}', subject="settings.json", format="jsonc")
