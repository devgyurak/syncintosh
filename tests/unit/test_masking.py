# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the masker: what leaves the process, and what comes back."""

from __future__ import annotations

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
