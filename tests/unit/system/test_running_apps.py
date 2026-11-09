# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `lsappinfo`-backed running-app check.

The two real outputs were checked on the build this was written against: a running app prints its ASN line
(`ASN:0x0-0x19019-"Finder":`) and a missing one prints a bare newline, both with exit status 0. Anything else is
"cannot tell", and a caller that must not write under an open app treats that as running.
"""

from __future__ import annotations

import subprocess

import pytest

from syncintosh.system.running_apps import LsappinfoRunningApps


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        (_Completed('ASN:0x0-0x19019-"Finder":\n'), True),
        (_Completed("\n"), False),
        (_Completed(""), False),
        (_Completed("", returncode=1), None),
        (_Completed("lsappinfo: could not connect\n"), None),
        (_Completed('ASN:0x0-0x19019-"Finder":\n', returncode=2), None),
    ],
)
def test_the_answer_follows_the_real_output(monkeypatch, completed: _Completed, expected: bool | None) -> None:
    # given
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: completed)

    # when
    running = LsappinfoRunningApps().running("com.example.app")

    # then
    assert running is expected


@pytest.mark.parametrize("error", [FileNotFoundError("lsappinfo"), subprocess.TimeoutExpired("lsappinfo", 3.0)])
def test_a_query_that_cannot_run_is_undeterminable(monkeypatch, error: BaseException) -> None:
    # given
    def _raise(*_a, **_k):
        raise error

    monkeypatch.setattr(subprocess, "run", _raise)

    # when & then
    assert LsappinfoRunningApps().running("com.example.app") is None


@pytest.mark.parametrize("bundle_id", ["", "a b", "x;y", "-v"])
def test_a_bundle_id_with_an_odd_shape_is_never_passed_on(monkeypatch, bundle_id: str) -> None:
    # given
    def _raise(*_a, **_k):
        raise AssertionError("must not run")

    monkeypatch.setattr(subprocess, "run", _raise)

    # when & then
    assert LsappinfoRunningApps().running(bundle_id) is None


def test_the_query_is_a_fixed_argv_naming_the_bundle_id(monkeypatch) -> None:
    # given
    captured: list[list[str]] = []

    def _run(argv, **_kwargs) -> _Completed:
        captured.append(argv)
        return _Completed("\n")

    monkeypatch.setattr(subprocess, "run", _run)

    # when
    LsappinfoRunningApps().running("com.example.app")

    # then
    assert captured == [["lsappinfo", "find", "bundleid=com.example.app"]]
