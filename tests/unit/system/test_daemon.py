# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the launchd plist builder and installer.

The load-bearing property is that `install` rewrites the plist with the **current** entry point
every time, so a stale path cannot survive (``docs/design/README.md``, launchd). No test calls
`launchctl`; the runner is injected.
"""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from syncintosh.core.errors import DaemonError
from syncintosh.system.daemon import PLIST_LABEL, LaunchdDaemon, build_plist


class _FakeRunner:
    """A `subprocess.run` shape that records its argv and can fail one launchctl subcommand."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_on = fail_on

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(argv))
        failed = self._fail_on is not None and self._fail_on in argv
        return subprocess.CompletedProcess(argv, 1 if failed else 0, stdout=b"", stderr=b"boom" if failed else b"")


def test_build_plist_runs_watch_with_the_poll(tmp_path) -> None:
    # when
    payload = plistlib.loads(build_plist(entry_point="/opt/homebrew/bin/syncintosh", poll_seconds=600))

    # then
    assert payload["Label"] == PLIST_LABEL
    assert payload["ProgramArguments"] == ["/opt/homebrew/bin/syncintosh", "watch", "--poll", "600"]
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert "PATH" in payload["EnvironmentVariables"]


def test_install_writes_and_records_the_entry_point(tmp_path) -> None:
    # given
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner())

    # when
    daemon.install(entry_point="/opt/homebrew/bin/syncintosh", poll_seconds=300)

    # then
    assert daemon.recorded_entry_point() == "/opt/homebrew/bin/syncintosh"


def test_install_rewrites_a_stale_entry_point(tmp_path) -> None:
    # given — an older install embedded a different interpreter
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner())
    daemon.install(entry_point="/usr/bin/python3.11-old", poll_seconds=300)

    # when
    daemon.install(entry_point="/opt/homebrew/bin/syncintosh", poll_seconds=300)

    # then — the current path replaces it; nothing stale survives
    assert daemon.recorded_entry_point() == "/opt/homebrew/bin/syncintosh"


def test_install_boots_out_then_bootstraps(tmp_path) -> None:
    # given
    runner = _FakeRunner()

    # when
    LaunchdDaemon(tmp_path, run=runner).install(entry_point="/x/syncintosh", poll_seconds=300)

    # then
    subcommands = [call[1] for call in runner.calls if call[0] == "launchctl"]
    assert subcommands == ["bootout", "bootstrap"]


def test_install_raises_when_bootstrap_fails(tmp_path) -> None:
    # given — the plist is written but launchd refuses to load it
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner(fail_on="bootstrap"))

    # when & then
    with pytest.raises(DaemonError):
        daemon.install(entry_point="/x/syncintosh", poll_seconds=300)


def test_uninstall_removes_the_plist(tmp_path) -> None:
    # given
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner())
    daemon.install(entry_point="/x/syncintosh", poll_seconds=300)

    # when
    daemon.uninstall()

    # then
    assert daemon.plist_path.exists() is False
    assert daemon.recorded_entry_point() is None


def test_uninstalling_without_a_plist_is_not_an_error(tmp_path) -> None:
    # given
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner())

    # when & then
    daemon.uninstall()
    assert daemon.recorded_entry_point() is None


def test_is_loaded_reports_the_launchd_state(tmp_path) -> None:
    # given — `launchctl print` succeeds
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner())

    # when & then
    assert daemon.is_loaded() is True


def test_is_loaded_is_unknown_when_launchctl_is_absent(tmp_path) -> None:
    # given — `launchctl` is not installed
    def missing(argv, **kwargs):
        raise FileNotFoundError("launchctl")

    daemon = LaunchdDaemon(tmp_path, run=missing)

    # when & then
    assert daemon.is_loaded() is None


def test_a_missing_launchctl_is_a_domain_error(tmp_path) -> None:
    # given — `launchctl` is not installed
    def missing(argv, **kwargs):
        raise FileNotFoundError("launchctl")

    daemon = LaunchdDaemon(tmp_path, run=missing)

    # when & then — no raw traceback
    with pytest.raises(DaemonError):
        daemon.install(entry_point="/x/syncintosh", poll_seconds=300)


class _FlakyBootstrap:
    """A runner whose first `bootstrap` fails transiently, then succeeds."""

    def __init__(self) -> None:
        self.bootstraps = 0

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[bytes]:
        if argv[1] == "bootstrap":
            self.bootstraps += 1
            code = 1 if self.bootstraps < 2 else 0
        else:
            code = 0
        return subprocess.CompletedProcess(argv, code, stdout=b"", stderr=b"5: Input/output error")


def test_install_retries_a_transient_bootstrap_failure(tmp_path) -> None:
    # given — the first bootstrap fails right after the bootout
    runner = _FlakyBootstrap()

    # when
    LaunchdDaemon(tmp_path, run=runner).install(entry_point="/x/syncintosh", poll_seconds=300)

    # then — it retried and settled
    assert runner.bootstraps == 2


def test_recorded_entry_point_is_none_for_a_corrupt_plist(tmp_path) -> None:
    # given — a plist-shaped path that is not a plist
    daemon = LaunchdDaemon(tmp_path, run=_FakeRunner())
    daemon.plist_path.parent.mkdir(parents=True, exist_ok=True)
    daemon.plist_path.write_bytes(b"not a plist")

    # when & then
    assert daemon.recorded_entry_point() is None
