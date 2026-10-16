# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the machine configuration: the frozen model and its file form."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.core.config import LocalItem, SyncConfig, allows
from syncintosh.core.errors import ConfigError
from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.models import Scope
from syncintosh.system.config import load_config, save_config

GOOD_ITEM = LocalItem.create(path=".zshrc", preset="zsh", scope="common", format="text")
RECORD_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def _record(path: str = ".zshrc", scope: Scope = "common", machine: str | None = None) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=RECORD_ID, path=path, preset="zsh", scope=scope, machine=machine, format="text", sha256="a" * 64
    )


def _config() -> SyncConfig:
    return SyncConfig.create(remote="git@example.com:me/settings.git", machine_id="mac-studio", items=(GOOD_ITEM,))


def test_an_item_path_must_be_normalized() -> None:
    # when & then
    with pytest.raises(ConfigError):
        LocalItem.create(path="../evil", preset="zsh", scope="common", format="text")


def test_a_machine_scope_item_must_name_its_machine() -> None:
    # when & then
    with pytest.raises(ConfigError):
        LocalItem.create(path=".zshrc", preset="zsh", scope="machine", format="text")


def test_a_machine_scope_item_must_name_this_machine() -> None:
    # given
    foreign = LocalItem.create(path=".zshrc", preset="zsh", scope="machine", machine="other-mac", format="text")

    # when & then
    with pytest.raises(ConfigError):
        SyncConfig.create(remote="r", machine_id="mac-studio", items=(foreign,))


@pytest.mark.parametrize(("remote", "machine_id"), [("", "mac"), ("r", "")])
def test_the_remote_and_machine_id_must_be_non_empty(remote: str, machine_id: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        SyncConfig.create(remote=remote, machine_id=machine_id, items=())


def test_a_config_round_trips_through_its_file(tmp_path: Path) -> None:
    # given
    config = _config()
    path = tmp_path / "config.json"

    # when
    save_config(config, path)

    # then
    assert load_config(path) == config


def test_a_machine_scope_item_round_trips_with_its_machine(tmp_path: Path) -> None:
    # given
    item = LocalItem.create(path=".zshrc", preset="zsh", scope="machine", machine="mac-studio", format="text")
    config = SyncConfig.create(remote="r", machine_id="mac-studio", items=(item,))
    path = tmp_path / "config.json"

    # when
    save_config(config, path)

    # then
    assert load_config(path) == config


def test_a_missing_config_is_a_domain_error(tmp_path: Path) -> None:
    # when & then
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")


def test_the_allowlist_admits_a_tracked_path() -> None:
    # given
    config = _config()

    # when & then
    assert allows(config, _record()) is True


def test_the_allowlist_refuses_a_path_this_machine_does_not_track() -> None:
    # given — the record arrived from the remote, and the local allowlist never named it
    config = _config()

    # when & then
    assert allows(config, _record(path="Library/LaunchAgents/com.evil.plist")) is False


def test_the_allowlist_folds_two_spellings_of_one_path() -> None:
    # given
    config = _config()

    # when & then
    assert allows(config, _record(path=".Zshrc")) is True


def test_the_allowlist_refuses_another_machines_record() -> None:
    # given
    config = _config()

    # when & then
    assert allows(config, _record(scope="machine", machine="other-mac")) is False


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"remote":"r","machine_id":"m"}',
        '{"remote":"r","machine_id":"m","items":{}}',
        '{"remote":"r","machine_id":"m","items":[7]}',
        '{"remote":"r","machine_id":"m","items":[{"path":".zshrc","preset":"zsh","scope":"common","format":"text","x":1}]}',
        '{"remote":"r","machine_id":"m","items":[{"path":".zshrc"}]}',
        '{"machine_id":"m","items":[]}',
    ],
)
def test_a_malformed_config_is_a_domain_error(tmp_path: Path, payload: str) -> None:
    # given
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")

    # when & then
    with pytest.raises(ConfigError):
        load_config(path)
