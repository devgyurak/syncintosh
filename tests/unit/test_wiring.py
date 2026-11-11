# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI wiring: what the builders construct, and what they must not touch."""

from __future__ import annotations

import inspect
from pathlib import Path

from syncintosh.cli import wiring
from syncintosh.services import SyncPorts
from syncintosh.system import profile_index
from syncintosh.system.paths import MachinePaths


def _paths(tmp_path: Path) -> MachinePaths:
    home = tmp_path / "home"
    return MachinePaths(home=home, config_dir=home / ".config" / "syncintosh", state_dir=home / "state")


def test_the_read_side_builder_does_not_create_the_scan_allowlist(tmp_path) -> None:
    # given — a machine that was never set up
    paths = _paths(tmp_path)

    # when
    wiring.machine_reads(paths)

    # then — `diff` and `doctor` stay read-only: only the full builder materializes the allowlist
    assert paths.scan_allow_file.exists() is False


def test_the_read_side_builder_builds_the_profile_index_once(tmp_path, monkeypatch) -> None:
    # given
    built: list[object] = []
    real = profile_index.SystemProfileIndex

    def counting(*args, **kwargs):
        built.append(object())
        return real(*args, **kwargs)

    monkeypatch.setattr(profile_index, "SystemProfileIndex", counting)

    # when
    wiring.machine_reads(_paths(tmp_path))

    # then
    assert len(built) == 1


def test_no_sync_port_has_a_default() -> None:
    # given
    parameters = inspect.signature(SyncPorts.create).parameters.values()

    # when
    defaulted = [p.name for p in parameters if p.default is not inspect.Parameter.empty]

    # then — a port a caller forgot is an error, not a silent null object
    assert defaulted == []
