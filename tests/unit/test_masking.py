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
    (masked,) = masker.mask("hi José, José2")

    # then
    assert "José" not in masked.text


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
        lambda masked: masked.text.replace(masked.values[0][0], "value-was-here"),
        lambda masked: masked.text + " " + masked.values[0][0],
        lambda masked: masked.text.replace("<<snt:", "<<snt:0000000", 1),
    ],
    ids=["dropped-token", "duplicated-token", "altered-token"],
)
def test_a_tampered_proposal_is_refused(tamper) -> None:
    # given
    (masked,) = _masker().mask(TEXT)

    # when
    restored = _masker().unmask(tamper(masked), masked)

    # then
    assert restored is None


def test_a_masker_with_no_home_or_username_masks_only_secrets_and_hosts() -> None:
    # given
    masker = TextMasker(home="", username="", internal_domains=())

    # when
    (masked,) = masker.mask(f"owner {USER} at {HOME}")

    # then — nothing empty is treated as a literal to mask
    assert masked.text == f"owner {USER} at {HOME}"
