# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the `mdfind`-backed app detector.

Every case pins the three-valued contract: `True`/`False` only when Spotlight actually answered,
`None` when it did not — because a detector that guessed `False` would defer a file on a machine
that has the app.
"""

from __future__ import annotations

import subprocess

import pytest

from syncintosh.system.app_detector import MdfindAppDetector


class _Completed:
    """The slice of `subprocess.CompletedProcess` the detector reads."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _run_returning(result: _Completed):
    def _run(*_args, **_kwargs) -> _Completed:
        return result

    return _run


def _run_raising(error: BaseException):
    def _run(*_args, **_kwargs):
        raise error

    return _run


def test_a_bundle_path_in_the_output_is_installed(monkeypatch) -> None:
    # given — `mdfind` prints one path per matching bundle
    monkeypatch.setattr(subprocess, "run", _run_returning(_Completed("/Applications/Visual Studio Code.app\n")))

    # when
    installed = MdfindAppDetector().installed("com.microsoft.VSCode")

    # then
    assert installed is True


def test_empty_output_is_not_installed(monkeypatch) -> None:
    # given — `mdfind` exits 0 with no output when nothing matches
    monkeypatch.setattr(subprocess, "run", _run_returning(_Completed("")))

    # when
    installed = MdfindAppDetector().installed("com.microsoft.VSCode")

    # then
    assert installed is False


def test_a_nonzero_exit_is_undeterminable(monkeypatch) -> None:
    # given
    monkeypatch.setattr(subprocess, "run", _run_returning(_Completed("", returncode=1)))

    # when
    installed = MdfindAppDetector().installed("com.microsoft.VSCode")

    # then — `None`, never `False`: the caller must fall back to the directory heuristic
    assert installed is None


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("mdfind"), subprocess.TimeoutExpired("mdfind", 5.0)],
)
def test_a_query_that_cannot_run_is_undeterminable(error, monkeypatch) -> None:
    # given — no `mdfind` on the system, or a query that did not finish in time
    monkeypatch.setattr(subprocess, "run", _run_raising(error))

    # when
    installed = MdfindAppDetector().installed("com.microsoft.VSCode")

    # then
    assert installed is None


def test_the_query_names_the_bundle_id(monkeypatch) -> None:
    # given
    captured: list[list[str]] = []

    def _run(argv, **_kwargs) -> _Completed:
        captured.append(argv)
        return _Completed("")

    monkeypatch.setattr(subprocess, "run", _run)

    # when
    MdfindAppDetector().installed("com.docker.docker")

    # then — a fixed argv list, never a shell string
    assert captured[0][0] == "mdfind"
    assert "com.docker.docker" in captured[0][1]
