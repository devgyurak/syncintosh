# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the masker: what leaves the process, and what comes back."""

from __future__ import annotations

import base64

import pytest

from syncintosh.core.advice import MaskedText
from syncintosh.system.masking import TextMasker

HOME = "/Users/alice"
USER = "alice"
INTERNAL = "git.corp"
SECRET = "AKIAABCDEFGHIJKLMNOP"
TEXT = f'export KEY="{SECRET}"\nsource {HOME}/.env\nurl https://{INTERNAL}/repo\nowner {USER}\n'


def _masker() -> TextMasker:
    return TextMasker(home=HOME, username=USER, internal_domains=())


def test_masking_then_unmasking_restores_the_original_text() -> None:
    # given
    (masked,) = _masker().mask(TEXT)

    # when
    restored = _masker().unmask(masked.text, masked)

    # then
    assert restored == TEXT


def test_no_secret_host_home_or_username_survives_masking() -> None:
    # given
    (masked,) = _masker().mask(TEXT)

    # when
    leaked = [needle for needle in (SECRET, HOME, INTERNAL, USER) if needle in masked.text]

    # then
    assert leaked == []


@pytest.mark.parametrize(
    "text",
    [
        "fetch http://bob:hunter2@proxy.example.com:8080/x",
        "Authorization: Bearer abcdef0123456789abcdef",
    ],
    ids=["url-credentials", "bearer-token"],
)
def test_a_credential_the_structured_scan_knows_is_masked(text: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then
    assert "hunter2" not in masked.text
    assert "abcdef0123456789abcdef" not in masked.text


def test_a_non_ascii_username_is_masked() -> None:
    # given
    masker = TextMasker(home="", username="José", internal_domains=())

    # when
    (masked,) = masker.mask("hi José, not José2")

    # then — the standalone name is masked; the longer word is a different word and is left alone
    assert "hi José," not in masked.text
    assert "José2" in masked.text


def test_overlapping_matches_are_masked_as_one_value() -> None:
    # given — the home path is also the start of an internal host, so their matches overlap
    (masked,) = _masker().mask("/Users/alice.corp.lan/x")

    # when & then — no fragment of the host's tail is left behind
    assert "corp" not in masked.text
    assert "lan" not in masked.text


def test_a_public_host_is_left_alone() -> None:
    # given
    (masked,) = _masker().mask("see https://api.example.com/docs")

    # when & then
    assert masked.text == "see https://api.example.com/docs"


def test_a_configured_internal_domain_is_masked() -> None:
    # given
    masker = TextMasker(home=HOME, username=USER, internal_domains=("example.com",))

    # when
    (masked,) = masker.mask("ping wiki.example.com")

    # then
    assert "wiki.example.com" not in masked.text


def test_texts_masked_together_share_one_table() -> None:
    # given
    base, local = _masker().mask(f"a {SECRET}", f"b {SECRET}")

    # when & then — the same value in two texts is two tokens, and both texts carry the whole table
    assert base.values == local.values
    assert len(base.values) == 2
    assert base.values[0][0] != base.values[1][0]


def test_each_masked_text_is_a_masked_text() -> None:
    # when
    (masked,) = _masker().mask(TEXT)

    # then
    assert isinstance(masked, MaskedText)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda masked: masked.text + " " + masked.values[0][0],
        lambda masked: masked.text.replace("<<snt:", "<<snt:0000000", 1),
    ],
    ids=["duplicated-token", "altered-token"],
)
def test_a_tampered_proposal_is_refused(tamper) -> None:
    # given
    (masked,) = _masker().mask(TEXT)

    # when
    restored = _masker().unmask(tamper(masked), masked)

    # then
    assert restored is None


def test_a_proposal_that_drops_a_token_restores_without_that_value() -> None:
    # given
    (masked,) = _masker().mask(TEXT)
    first_token = masked.values[0][0]

    # when
    restored = _masker().unmask(masked.text.replace(first_token, ""), masked)

    # then — the dropped value is gone from the result, and the person sees that in the approval screen
    assert restored is not None
    assert SECRET not in restored


def test_a_whole_word_username_is_masked_but_not_a_substring_of_another_word() -> None:
    # given
    masker = TextMasker(home="", username="al", internal_domains=())

    # when
    (masked,) = masker.mask("owner al, not false")

    # then
    assert "false" in masked.text
    assert "owner al" not in masked.text


@pytest.mark.parametrize(
    "text",
    ['"password": "hunter2"', "GITHUB_TOKEN=hunter2", "api_key: hunter2"],
    ids=["json", "shell", "yaml"],
)
def test_a_value_under_a_credential_key_is_masked(text: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then
    assert "hunter2" not in masked.text


def test_a_single_label_host_in_a_url_is_masked() -> None:
    # when
    (masked,) = _masker().mask("http://mac-mini:11434/v1")

    # then
    assert "mac-mini" not in masked.text


def test_a_local_copy_with_a_base_token_round_trips_through_one_table() -> None:
    # given — the base carries the username, so the shared table holds a token the local text lacks
    base, local, remote = _masker().mask(f"owner {USER}", f"x = {SECRET}", "y = 1")

    # when
    restored = _masker().unmask(local.text, local)

    # then
    assert restored == f"x = {SECRET}"
    assert base.values == local.values == remote.values


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"mysql.password": "correct horse battery"}', "correct horse battery"),
        ('{"password": "a,b;c}d"}', "a,b;c}d"),
        ('export API_KEY="abc def ghi"', "abc def ghi"),
        ('{"github.token":\n  "ghx_abcdef"}', "ghx_abcdef"),
        ('{"auth": "Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
        ("export TOKEN=abc def ghi", "abc def ghi"),
        ("Cookie: session=abc123", "abc123"),
    ],
    ids=[
        "dotted-key-spaces",
        "quoted-delims",
        "shell-quoted-spaces",
        "newline",
        "auth-basic",
        "shell-unquoted",
        "cookie",
    ],
)
def test_a_value_under_a_credential_key_is_masked_whole(text: str, secret: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then — the whole value is hidden, not only its first word
    assert secret not in masked.text


def test_a_value_under_an_ordinary_key_is_left_alone() -> None:
    # when
    (masked,) = _masker().mask('{"author": "Jane"}')

    # then
    assert masked.text == '{"author": "Jane"}'


@pytest.mark.parametrize("url", ["http://134744072/v1", "http://0x08080808/v1"], ids=["decimal", "hex"])
def test_a_numeric_public_host_is_not_masked_as_internal(url: str) -> None:
    # when — the resolver reads these as 8.8.8.8, a public address, so the host is not an internal name
    (masked,) = _masker().mask(url)

    # then
    assert masked.text == url


def test_a_password_inside_a_nested_object_is_masked() -> None:
    # when — the nested `pass` key is a credential too
    (masked,) = _masker().mask('{"auth": {"user": "u", "pass": "hunter2"}}')

    # then
    assert "hunter2" not in masked.text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("headers:\n  Authorization: >\n    Bearer\n    eyJhbGciOiJIUzI1NiJ9abcdef\n", "eyJhbGciOiJIUzI1NiJ9abcdef"),
        ("Authorization: |\n  Token abc123\n", "abc123"),
        ("Authorization: >-\n  Bearer\n\n  tail-token-xyz\n", "tail-token-xyz"),
        ("Authorization: >\n" + "  note\n" * 70 + "  Token tail-token-xyz\n", "tail-token-xyz"),
        ("Authorization: |\n" + "\n" * 70_000 + "  Token tail-token-xyz\n", "tail-token-xyz"),
        ('Authorization: !!str "Token tail-token-xyz"\n', "tail-token-xyz"),
    ],
    ids=["split-scheme-and-token", "short-token", "blank-line-inside", "past-the-line-cap", "past-the-byte-cap", "tag"],
)
def test_a_token_on_a_yaml_block_continuation_line_is_masked(text: str, secret: str) -> None:
    # `_KEYED_VALUE` alone would hide only the `>` after the key
    # when
    (masked,) = _masker().mask(text)

    # then
    assert secret not in masked.text
    assert _masker().unmask(masked.text, masked) == text


@pytest.mark.parametrize("scheme", ["Basic", "basic", "BASIC"])
def test_a_basic_credential_with_no_digit_and_no_padding_is_masked(scheme: str) -> None:
    # given — the digit-free shape the scan refuses by decoding it (`nbH:QDDtIPyn`)
    token = "bmJIOlFERHRJUHlu"
    text = f"curl -u x {scheme} {token}\n"

    # when
    (masked,) = _masker().mask(text)

    # then
    assert token not in masked.text


def test_the_tail_of_a_basic_credential_past_the_width_cap_is_masked() -> None:
    # given — a digit-free credential longer than the 256-character cap the pattern stops at
    token = base64.b64encode(b"user:" + b"a" * 300).decode()

    # when
    (masked,) = _masker().mask(f"x Basic {token}\n")

    # then
    assert token[-8:] not in masked.text


# --------------------------------------------------------------------------- AWS secret access key (#63)

AWS_VALUE = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ('{"args": {"text": "export AWS_SECRET_ACCESS_KEY=' + AWS_VALUE + '\\u000D"}}', "AWS_SECRET_ACCESS_KEY"),
        ("export AWS_SECRET_ACCESS_KEY=" + AWS_VALUE + "\n", "AWS_SECRET_ACCESS_KEY"),
        ('{"aws_secret_access_key": "' + AWS_VALUE + '"}', "aws_secret_access_key"),
        ("aws_secret_access_key = " + AWS_VALUE + "\n", "aws_secret_access_key"),
        ("<key>SecretAccessKey</key>\n<string>" + AWS_VALUE + "</string>", "SecretAccessKey"),
        ('{"SecretAccessKey": "' + AWS_VALUE + '"}', "SecretAccessKey"),
        ("export TF_VAR_aws_secret_access_key=" + AWS_VALUE + "\n", "TF_VAR_aws_secret_access_key"),
        ("aws_session_token = " + AWS_VALUE + AWS_VALUE + "\n", "aws_session_token"),
        ("secretAccessKey: " + AWS_VALUE + "\n", "secretAccessKey"),
    ],
    ids=["keybinding", "shell", "json", "toml", "plist", "camel-case", "prefixed", "session-token", "yaml-camel"],
)
def test_an_aws_secret_access_key_is_masked_and_its_name_stays_visible(text: str, name: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then — the value is hidden, the key name is not (the model has to know what it merges)
    assert (AWS_VALUE in masked.text, name in masked.text) == (False, True)


@pytest.mark.parametrize("text", ["tokenizer: whitespace", "passthrough: true", "authorName: Jane Doe"])
def test_a_name_that_only_starts_with_a_keyword_is_left_alone(text: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then
    assert masked.text == text


def test_overlapping_rules_leave_the_aws_key_name_visible() -> None:
    # given — both rules fire on the same line: the keyed rule and the AWS rule overlap on the value only
    text = "AWS_SECRET_ACCESS_KEY=" + AWS_VALUE

    # when
    (masked,) = _masker().mask(text)

    # then — the merged span starts at the value, so the name is intact and exactly one token replaces it
    assert masked.text.startswith("AWS_SECRET_ACCESS_KEY=") and len(masked.values) == 1


def test_an_aws_value_on_a_yaml_block_line_is_a_known_gap() -> None:
    # given — D6: the value is on the line after `|`; neither rule reaches it
    text = "aws_secret_access_key: |\n  " + AWS_VALUE + "\n"

    # when
    (masked,) = _masker().mask(text)

    # then
    assert AWS_VALUE in masked.text


@pytest.mark.parametrize(
    "text",
    [
        '"editor.semanticTokenColorCustomizations": {"enabled": true}',
        '"tokenColorCustomizations": [1, 2]',
        '{"auth": {"enabled": true}}',
    ],
    ids=["object", "array", "auth-object"],
)
def test_an_object_or_array_under_a_credential_like_key_keeps_its_brackets(text: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then — the opening bracket is structure, not a value
    assert masked.text == text


@pytest.mark.parametrize(
    ("text", "tail"),
    [
        ("<key>SecretAccessKey</key><string>" + AWS_VALUE + "-tail9</string>", "-tail9"),
        ("<SecretAccessKey>" + AWS_VALUE + "-tail9</SecretAccessKey>", "-tail9"),
    ],
    ids=["plist", "element"],
)
def test_the_tail_of_an_aws_value_past_the_base64_run_is_masked(text: str, tail: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then
    assert tail not in masked.text


@pytest.mark.parametrize(
    "text",
    [
        '{"SecretAccessKey":\n  "' + AWS_VALUE + '"}',
        "aws configure set aws_secret_access_key " + AWS_VALUE,
        "launchctl setenv AWS_SECRET_ACCESS_KEY " + AWS_VALUE,
        "AWS_SECRET_ACCESS_KEY := " + AWS_VALUE,
        "<SecretAccessKey>" + AWS_VALUE + "</SecretAccessKey>",
    ],
    ids=["wrapped", "cli-config", "launchctl", "make-assign", "element"],
)
def test_an_aws_secret_access_key_in_a_variant_spelling_is_masked(text: str) -> None:
    # when
    (masked,) = _masker().mask(text)

    # then
    assert AWS_VALUE not in masked.text
