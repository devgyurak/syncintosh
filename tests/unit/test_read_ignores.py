# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for reading the user's ignore list at the cycle entry.

A list that cannot be read is not an empty list: a profile's `settings.json` obeys the default file's list,
and an empty one would seal the keys the user meant to keep local (``docs/design/presets.md``).
"""

from __future__ import annotations

import json

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.ignore import IGNORED_SETTINGS_KEY, NO_IGNORES, IgnoreSpec
from syncintosh.presets import BuiltinCatalog, vscode
from syncintosh.services.sync._shared import _IGNORE_LIST_UNREADABLE, _read_ignores
from syncintosh.system.live_files import SystemLiveFiles


class _Deny:
    def __init__(self, *, denied: bool = False, escapes: bool = False) -> None:
        self._denied = denied
        self._escapes = escapes

    def is_denied(self, home: str, relpath: str) -> bool:
        return self._denied

    def escapes(self, home: str, relpath: str) -> bool:
        return self._escapes


def _read(home, deny=None):
    return _read_ignores(
        BuiltinCatalog.default(), {"jsonc": JsoncAdapter()}, SystemLiveFiles(), deny or _Deny(), str(home)
    )[vscode.SETTINGS]


def _settings(home, text: str):
    target = home / vscode.SETTINGS
    target.parent.mkdir(parents=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_a_file_that_is_not_there_has_no_list(tmp_path) -> None:
    # when & then
    assert _read(tmp_path) is NO_IGNORES


def test_a_readable_list_is_read(tmp_path) -> None:
    # given
    _settings(tmp_path, json.dumps({IGNORED_SETTINGS_KEY: ["editor.fontSize"]}))

    # when
    spec = _read(tmp_path)

    # then
    assert isinstance(spec, IgnoreSpec) and spec.keys == {"editor.fontSize"}


def test_a_symlinked_list_file_cannot_be_used(tmp_path) -> None:
    # given
    real = tmp_path / "elsewhere.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / vscode.SETTINGS
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    # when & then
    assert _read(tmp_path) == _IGNORE_LIST_UNREADABLE


@pytest.mark.parametrize("deny", [_Deny(denied=True), _Deny(escapes=True)])
def test_a_denied_or_escaping_list_file_cannot_be_used(tmp_path, deny) -> None:
    # given
    _settings(tmp_path, json.dumps({IGNORED_SETTINGS_KEY: ["editor.fontSize"]}))

    # when & then
    assert _read(tmp_path, deny) == _IGNORE_LIST_UNREADABLE
