# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the git error detail: credentials must never reach a log (AGENTS.md §4)."""

from __future__ import annotations

from syncintosh.vcs.git import _MAX_DETAIL, _redact


def test_url_credentials_are_redacted() -> None:
    # given — git echoes the remote URL, and an HTTPS URL can carry a token
    message = "fatal: could not read from https://user:s3cr3t@example.com/repo.git"

    # when
    detail = _redact(message)

    # then
    assert "s3cr3t" not in detail
    assert "://<redacted>@" in detail


def test_an_ssh_style_url_without_credentials_is_left_alone() -> None:
    # given
    message = "fatal: could not read from git@example.com:me/repo.git"

    # when & then — no `://user@` userinfo to strip
    assert _redact(message) == message


def test_a_long_detail_is_bounded() -> None:
    # when
    detail = _redact("x" * (_MAX_DETAIL * 2))

    # then
    assert len(detail) == _MAX_DETAIL
