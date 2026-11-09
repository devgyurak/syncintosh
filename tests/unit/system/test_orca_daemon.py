# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for counting Orca's terminal host (`orcad`) as Orca running.

Orca's terminals live in a process that outlives its window, recorded in `daemon/daemon-v<N>.pid` as a JSON object with
a `pid`. The pid file's shape is what the inspector showed on a real Orca 1.4.219 (`pid`, `startedAtMs`, `entryPath`,
`appVersion`, `spawnerExecPath`, `cgroupUnit`, `launchNonce`); the values here are invented. Processes are faked, so no
real process is asked about except in the one test of the default lookup, which asks about this test's own.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from syncintosh.system.orca_daemon import DAEMON_DIRECTORY, OrcaDaemonRunningApps, executable_path

ORCA = "com.stablyai.orca"
INSIDE = "/Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper"
ELSEWHERE = "/usr/bin/python3"


class _Inner:
    """The `lsappinfo` answer, fixed, and a record of who was asked."""

    def __init__(self, answer: bool | None) -> None:
        self._answer = answer
        self.asked: list[str] = []

    def running(self, bundle_id: str) -> bool | None:
        self.asked.append(bundle_id)
        return self._answer


def _pid_file(home: Path, content: str | dict, name: str = "daemon-v38.pid") -> None:
    directory = home / DAEMON_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (directory / name).write_text(text, encoding="utf-8")


def _record(pid: object = 4242) -> dict:
    return {"pid": pid, "startedAtMs": 1.0, "entryPath": "/x", "appVersion": "1.4.219", "launchNonce": "n"}


def _paths(table: dict[int, str | BaseException]) -> Callable[[int], str]:
    def _lookup(pid: int) -> str:
        found = table.get(pid, ProcessLookupError())
        if isinstance(found, BaseException):
            raise found
        return found

    return _lookup


def _apps(home: Path, inner: _Inner, table: dict[int, str | BaseException]) -> OrcaDaemonRunningApps:
    return OrcaDaemonRunningApps(inner, home=str(home), bundle_id=ORCA, process_path=_paths(table))


@pytest.mark.parametrize(
    ("app", "table", "expected"),
    [
        # the app itself is the answer when it runs; the daemon is not even looked for
        (True, {}, True),
        # the app has quit, but its daemon is alive and runs from inside the bundle
        (False, {4242: INSIDE}, True),
        # the app has quit and the pid file is stale: no such process
        (False, {}, False),
        # the pid was reused by a process that is not Orca's
        (False, {4242: ELSEWHERE}, False),
        # the process exists but its path cannot be read: cannot tell
        (False, {4242: PermissionError()}, None),
        # `lsappinfo` could not answer and the daemon is gone: still cannot tell
        (None, {}, None),
        # `lsappinfo` could not answer, but the daemon is alive: running
        (None, {4242: INSIDE}, True),
    ],
)
def test_the_app_and_its_daemon_are_asked_together(
    tmp_path, app: bool | None, table: dict[int, str | BaseException], expected: bool | None
) -> None:
    # given
    _pid_file(tmp_path, _record())

    # when
    running = _apps(tmp_path, _Inner(app), table).running(ORCA)

    # then
    assert running is expected


def test_no_daemon_directory_means_the_daemon_is_not_running(tmp_path) -> None:
    # given — Orca never started its terminal host on this machine
    apps = _apps(tmp_path, _Inner(False), {})

    # when & then
    assert apps.running(ORCA) is False


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1, 2]",
        "{}",
        json.dumps(_record(pid="4242")),
        json.dumps(_record(pid=0)),
        json.dumps(_record(pid=True)),
    ],
)
def test_a_pid_file_that_does_not_name_a_process_cannot_be_told(tmp_path, content: str) -> None:
    # given
    _pid_file(tmp_path, content)

    # when
    running = _apps(tmp_path, _Inner(False), {}).running(ORCA)

    # then — guessing "not running" would write under an open app
    assert running is None


def test_one_live_daemon_among_stale_files_is_running(tmp_path) -> None:
    # given — an old daemon version's file is stale, the current one's is alive
    _pid_file(tmp_path, _record(pid=111), name="daemon-v36.pid")
    _pid_file(tmp_path, _record(pid=4242), name="daemon-v38.pid")

    # when
    running = _apps(tmp_path, _Inner(False), {4242: INSIDE}).running(ORCA)

    # then
    assert running is True


def test_another_app_is_answered_by_inner_alone(tmp_path) -> None:
    # given — a live Orca daemon must not make an unrelated app look open
    _pid_file(tmp_path, _record())
    inner = _Inner(False)

    # when
    running = _apps(tmp_path, inner, {4242: INSIDE}).running("com.example.other")

    # then
    assert running is False
    assert inner.asked == ["com.example.other"]


def test_a_file_where_the_daemon_directory_should_be_holds_no_pid_files(tmp_path) -> None:
    # given — the daemon directory is a file, so listing it fails
    (tmp_path / "Library" / "Application Support" / "orca").mkdir(parents=True)
    (tmp_path / DAEMON_DIRECTORY).write_text("", encoding="utf-8")

    # when
    running = _apps(tmp_path, _Inner(False), {}).running(ORCA)

    # then
    assert running is False


@pytest.mark.skipif(sys.platform != "darwin", reason="the default lookup reads libproc, which only macOS has")
def test_the_default_lookup_finds_this_process() -> None:
    # when
    path = executable_path(os.getpid())

    # then — the same file the interpreter was started from, whatever uv or a venv calls it
    assert Path(path).samefile(sys.executable)


def test_a_platform_without_libproc_cannot_be_told_and_does_not_raise(tmp_path, monkeypatch) -> None:
    # given — a live pid file, but the library the default lookup loads is not there (as on Linux)
    def _missing(*_args, **_kwargs):
        raise OSError("/usr/lib/libproc.dylib: cannot open shared object file: No such file or directory")

    monkeypatch.setattr(ctypes, "CDLL", _missing)
    _pid_file(tmp_path, _record(pid=os.getpid()))
    apps = OrcaDaemonRunningApps(_Inner(False), home=str(tmp_path), bundle_id=ORCA)

    # when
    running = apps.running(ORCA)

    # then — "cannot tell", which defers the apply, instead of an error out of the sync
    assert running is None


def test_the_default_lookup_reports_a_process_that_is_gone() -> None:
    # given — a pid far beyond any real one
    gone = 2**22 + 12345

    # when & then
    with pytest.raises(ProcessLookupError):
        executable_path(gone)
