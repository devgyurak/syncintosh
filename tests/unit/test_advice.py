# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure rules that check what a model is shown and what it answers."""

from __future__ import annotations

import subprocess
import sys

import pytest

from syncintosh.core.advice import (
    Advice,
    is_internal_host,
    is_public_endpoint,
    parse_advice,
    placeholder,
    rationale_for_display,
    restore,
    sanitize_rationale,
)

TOKEN = placeholder("abcdef12", 0)
TABLE = ((TOKEN, "s3cret"),)


@pytest.mark.parametrize(
    "raw",
    [
        '{"choice": "local", "value": null, "rationale": "keep mine"}',
        '{"choice": "remote", "value": null, "rationale": "keep theirs"}',
        '{"choice": "proposal", "value": "x", "rationale": "merged"}',
    ],
)
def test_a_strict_answer_parses(raw: str) -> None:
    # when
    advice = parse_advice(raw)

    # then
    assert advice is not None
    assert advice.choice in ("local", "remote", "proposal")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"choice": "local", "value": null}',
        '{"choice": "local", "value": null, "rationale": "x", "extra": 1}',
        '{"choice": "delete", "value": null, "rationale": "x"}',
        '{"choice": "proposal", "value": null, "rationale": "x"}',
        '{"choice": "local", "value": "x", "rationale": "x"}',
        '{"choice": "proposal", "value": "x", "rationale": 3}',
    ],
)
def test_an_answer_outside_the_strict_shape_is_refused(raw: str) -> None:
    # when & then
    assert parse_advice(raw) is None


def test_terminal_escapes_are_removed_from_the_rationale() -> None:
    # given
    raw = '{"choice": "local", "value": null, "rationale": "\\u001b[2Jok\\u0007 done"}'

    # when
    advice = parse_advice(raw)

    # then
    assert advice == Advice(choice="local", rationale="ok done")


def test_an_advice_repr_never_shows_a_proposed_value() -> None:
    # when & then
    assert "hunter2" not in repr(Advice(choice="proposal", value="hunter2", rationale="r"))


def test_a_proposal_naming_each_token_once_is_restored() -> None:
    # when
    restored = restore(f"keep {TOKEN} here", TABLE)

    # then
    assert restored == "keep s3cret here"


@pytest.mark.parametrize(
    "proposed",
    [
        "no token at all",
        f"{TOKEN} {TOKEN}",
        f"{TOKEN} {placeholder('abcdef12', 9)}",
        f"{placeholder('ffffffff', 0)}",
    ],
    ids=["missing", "duplicated", "invented", "wrong-nonce"],
)
def test_a_proposal_that_does_not_name_the_tokens_exactly_is_refused(proposed: str) -> None:
    # when & then
    assert restore(proposed, TABLE) is None


def test_terminal_escapes_are_removed_by_sanitize() -> None:
    # when & then
    assert sanitize_rationale("a\x1b[31mb\x00c\nd\te") == "abc\nd\te"


def test_a_repeated_key_in_the_answer_is_refused() -> None:
    # given — `json` would keep only the last `choice`, so the first one would be silently ignored
    raw = '{"choice": "proposal", "choice": "local", "value": null, "rationale": "x"}'

    # when & then
    assert parse_advice(raw) is None


def test_a_deeply_nested_answer_is_refused_without_raising() -> None:
    # when & then — a model's answer must never crash the resolve run
    assert parse_advice("[" * 100000) is None


def test_c1_controls_and_bidi_overrides_are_removed_from_the_rationale() -> None:
    # when & then — C1 CSI can reach a terminal, and a bidi override can reorder what the person reads
    assert sanitize_rationale("a\x9b31mb\u202ec\u2066d") == "a31mbcd"


def test_a_rationale_for_display_restores_known_tokens_and_keeps_unknown_ones() -> None:
    # when
    shown = rationale_for_display(f"uses {TOKEN} and <<snt:00000000:7>>", TABLE)

    # then
    assert shown == "uses s3cret and <<snt:00000000:7>>"


@pytest.mark.parametrize(
    ("host", "configured", "internal"),
    [
        ("", (), False),
        ("localhost", (), True),
        ("127.0.0.1", (), True),
        ("10.1.2.3", (), True),
        ("192.168.0.9", (), True),
        ("172.20.1.1", (), True),
        ("::1", (), True),
        ("fe80::1", (), True),
        ("nas.local", (), True),
        ("git.internal", (), True),
        ("router.lan", (), True),
        ("ci.corp", (), True),
        ("printer.home.arpa", (), True),
        ("wiki.example.com", ("example.com",), True),
        ("api.example.com", (), False),
        ("8.8.8.8", (), False),
        ("example.com.evil", ("example.com",), False),
    ],
)
def test_internal_hosts_are_recognised(host: str, configured: tuple[str, ...], internal: bool) -> None:
    # when & then
    assert is_internal_host(host, configured) is internal


@pytest.mark.parametrize(
    ("base_url", "public"),
    [
        ("https://api.openai.com/v1", True),
        ("https://llm.example.com", True),
        ("http://127.0.0.1:8000/v1", False),
        ("http://nas.local:11434", False),
        ("http://10.0.0.5/v1", False),
    ],
)
def test_an_endpoint_is_public_unless_it_is_loopback_or_private(base_url: str, public: bool) -> None:
    # when & then
    assert is_public_endpoint(base_url) is public


def test_the_placeholder_format_is_fixed() -> None:
    # when & then
    assert placeholder("abcdef12", 3) == "<<snt:abcdef12:3>>"


def test_the_advice_module_imports_no_model_client() -> None:
    # when
    result = subprocess.run(
        [sys.executable, "-c", "import sys, syncintosh.core.advice; print('openai' in sys.modules)"],
        capture_output=True,
        text=True,
        check=True,
    )

    # then
    assert result.stdout.strip() == "False"
