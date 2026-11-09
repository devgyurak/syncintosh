# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the product-info reader: the release folder comes from the installed app, never from the disk.

The metadata shape (`dataDirectoryName`, `productVendor`) is the one a real JetBrains `product-info.json` has; the
layout here is a TEST-ONLY one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syncintosh.core.config_dirs import ConfigDirSpec
from syncintosh.system.app_config_dir import MAX_PRODUCT_INFO_BYTES, ProductInfoConfigDir

SPEC = ConfigDirSpec.create(
    home="Library/Application Support/JetBrains", prefix="Widget", vendor="JetBrains", bundle_id="com.example.widget"
)


def _app(root: Path, name: str, document: object | bytes) -> str:
    app = root / f"{name}.app"
    target = app / "Contents" / "Resources"
    target.mkdir(parents=True)
    data = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
    (target / "product-info.json").write_bytes(data)
    return str(app)


def _info(directory: object = "Widget2026.2", vendor: object = "JetBrains") -> dict[str, object]:
    return {"name": "Widget", "dataDirectoryName": directory, "productVendor": vendor}


def _reader(*found: str) -> ProductInfoConfigDir:
    return ProductInfoConfigDir(lambda _bundle: tuple(found))


def test_the_folder_comes_from_the_apps_own_metadata(tmp_path) -> None:
    # given
    app = _app(tmp_path, "Widget", _info())

    # when
    found = _reader(app).read(SPEC)

    # then
    assert (found.directory, found.problems) == ("Widget2026.2", ())


def test_the_highest_folder_on_disk_is_never_consulted(tmp_path) -> None:
    # given — a stale newer folder exists on disk (from a release that was uninstalled), the app says 2026.2
    (tmp_path / "home" / "Library" / "Application Support" / "JetBrains" / "Widget2026.4").mkdir(parents=True)
    app = _app(tmp_path, "Widget", _info("Widget2026.2"))

    # when
    found = _reader(app).read(SPEC)

    # then
    assert found.directory == "Widget2026.2"


@pytest.mark.parametrize(
    "document",
    [
        _info("Widget2026.2", vendor="Someone Else"),
        _info("Widget2026.2", vendor=None),
        _info("Other2026.2"),
        _info("Widget2026"),
        _info("Widget2026.2/../Evil2026.1"),
        _info("../Widget2026.2"),
        _info("Widget2026.2/x"),
        _info(""),
        _info(None),
        _info(2026.2),
        _info(["Widget2026.2"]),
        {"productVendor": "JetBrains"},
        ["not", "an", "object"],
        "a string",
    ],
)
def test_metadata_that_does_not_fit_the_layout_resolves_nothing(tmp_path, document: object) -> None:
    # given
    app = _app(tmp_path, "Widget", document)

    # when
    found = _reader(app).read(SPEC)

    # then — unresolved with a reason, so the caller defers and keeps the record
    assert (found.directory, found.reason != "") == (None, True)


@pytest.mark.parametrize("raw", [b"{", b"", b"\xff\xfe", b"[" * 100_000, b"x" * (MAX_PRODUCT_INFO_BYTES + 1)])
def test_a_broken_or_oversized_metadata_file_resolves_nothing(tmp_path, raw: bytes) -> None:
    # given
    app = _app(tmp_path, "Widget", raw)

    # when
    found = _reader(app).read(SPEC)

    # then
    assert found.directory is None


def test_an_app_without_a_metadata_file_resolves_nothing(tmp_path) -> None:
    # given
    bare = tmp_path / "Widget.app"
    bare.mkdir()

    # when
    found = _reader(str(bare)).read(SPEC)

    # then
    assert found.directory is None


def test_a_path_that_is_not_an_app_bundle_is_not_read(tmp_path) -> None:
    # given — a stray directory that Spotlight reported, with perfectly good metadata
    other = tmp_path / "not-an-app"
    (other / "Contents" / "Resources").mkdir(parents=True)
    (other / "Contents" / "Resources" / "product-info.json").write_text(json.dumps(_info()), encoding="utf-8")

    # when
    found = _reader(str(other)).read(SPEC)

    # then
    assert found.directory is None


def test_not_installed_and_cannot_ask_are_told_apart() -> None:
    # when
    absent = ProductInfoConfigDir(lambda _bundle: ()).read(SPEC)
    unknown = ProductInfoConfigDir(lambda _bundle: None).read(SPEC)

    # then
    assert absent.directory is None and unknown.directory is None
    assert absent.reason != unknown.reason


def test_with_several_installs_the_highest_release_wins_and_it_is_noted(tmp_path) -> None:
    # given — 2026.10 is higher than 2026.9 although it sorts lower as text
    old = _app(tmp_path / "a", "Widget", _info("Widget2026.9"))
    new = _app(tmp_path / "b", "Widget", _info("Widget2026.10"))
    bad = _app(tmp_path / "c", "Widget", _info("Widget9999.9", vendor="Other"))

    # when
    found = _reader(old, bad, new).read(SPEC)

    # then — the untrusted copy cannot win with a higher number
    assert found.directory == "Widget2026.10"
    assert [problem for problem in found.problems if "Widget2026.9" in problem] != []


def test_two_installs_of_one_release_are_not_a_problem(tmp_path) -> None:
    # given
    first = _app(tmp_path / "a", "Widget", _info())
    second = _app(tmp_path / "b", "Widget", _info())

    # when
    found = _reader(first, second).read(SPEC)

    # then
    assert (found.directory, found.problems) == ("Widget2026.2", ())


def test_the_locator_is_asked_for_the_layouts_bundle_id() -> None:
    # given
    asked: list[str] = []

    def locate(bundle_id: str) -> tuple[str, ...]:
        asked.append(bundle_id)
        return ()

    # when
    ProductInfoConfigDir(locate).read(SPEC)

    # then
    assert asked == ["com.example.widget"]


# --------------------------------------------------------------------------- the app's own settings sync (in memory)

SYNC_FILE = "Library/Application Support/JetBrains/Widget2026.2/options/settingsSync.xml"
_SYNC_ON = (
    '<application><component name="SettingsSyncSettings">'
    '<option name="syncEnabled" value="true" /></component></application>'
)
_SYNC_OFF = (
    '<application><component name="SettingsSyncSettings">'
    '<option name="migrationFromOldStorageChecked" value="true" /></component></application>'
)


def _sync_map(tmp_path: Path, content: bytes | None):
    app = _app(tmp_path, "Widget", _info())
    if content is not None:
        target = tmp_path / "home" / SYNC_FILE
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
    return ProductInfoConfigDir(lambda _bundle: (app,), home=tmp_path / "home").read(SPEC)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_SYNC_ON.encode(), (True, False)),
        (_SYNC_ON.replace("true", "TRUE").encode(), (True, False)),
        (_SYNC_ON.replace("true", "True").encode(), (True, False)),
        (_SYNC_OFF.encode(), (False, False)),
        (_SYNC_ON.replace("true", "false").encode(), (False, False)),
        # A missing file is the IDE default (off), not "cannot tell".
        (None, (False, False)),
        # A file that exists but cannot be read safely is unknown, never off.
        (b"<application", (False, True)),
        (b"", (False, True)),
        (b'<!DOCTYPE a [<!ENTITY x "y">]>' + _SYNC_ON.encode(), (False, True)),
        (_SYNC_ON.encode() + b" " * (64 * 1024), (False, True)),
        (b"\xff\xfe not utf-8", (False, True)),
        (('<?xml version="1.0" encoding="utf-16"?>' + _SYNC_ON).encode("utf-16"), (False, True)),
    ],
)
def test_sync_state_is_on_off_or_unreadable(tmp_path, content: bytes | None, expected: tuple[bool, bool]) -> None:
    # when
    found = _sync_map(tmp_path, content)

    # then
    assert (found.settings_sync_enabled, found.settings_sync_unreadable) == expected


def test_a_symlinked_sync_file_is_unreadable_not_off(tmp_path) -> None:
    # given
    app = _app(tmp_path, "Widget", _info())
    outside = tmp_path / "elsewhere.xml"
    outside.write_text(_SYNC_ON, encoding="utf-8")
    link = tmp_path / "home" / SYNC_FILE
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    # when
    found = ProductInfoConfigDir(lambda _bundle: (app,), home=tmp_path / "home").read(SPEC)

    # then
    assert (found.settings_sync_enabled, found.settings_sync_unreadable) == (False, True)


def test_without_a_home_the_sync_file_is_not_read(tmp_path) -> None:
    # given
    app = _app(tmp_path, "Widget", _info())

    # when
    found = _reader(app).read(SPEC)

    # then
    assert (found.settings_sync_enabled, found.settings_sync_unreadable) == (False, False)


def test_a_fifo_in_place_of_the_sync_file_does_not_hang_and_is_unreadable(tmp_path) -> None:
    # given
    app = _app(tmp_path, "Widget", _info())
    fifo = tmp_path / "home" / SYNC_FILE
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    # when
    found = ProductInfoConfigDir(lambda _bundle: (app,), home=tmp_path / "home").read(SPEC)

    # then
    assert (found.settings_sync_enabled, found.settings_sync_unreadable) == (False, True)


def test_a_huge_build_number_resolves_nothing_instead_of_raising(tmp_path) -> None:
    # given — int() of thousands of digits raises, which would abort the whole sync
    app = _app(tmp_path, "Widget", _info("Widget2026." + "9" * 5000))

    # when
    found = _reader(app).read(SPEC)

    # then
    assert (found.directory, found.reason != "") == (None, True)


def test_a_fifo_as_the_metadata_file_does_not_hang_the_read(tmp_path) -> None:
    # given — a named pipe with no writer: a plain open() would block forever
    app = tmp_path / "Widget.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    os.mkfifo(app / "Contents" / "Resources" / "product-info.json")

    # when
    found = _reader(str(app)).read(SPEC)

    # then
    assert found.directory is None
    assert [problem for problem in found.problems if "regular file" in problem] != []


def test_an_install_that_cannot_be_read_is_reported_beside_the_one_that_can(tmp_path) -> None:
    # given
    good = _app(tmp_path / "a", "Widget", _info("Widget2026.3"))
    broken = _app(tmp_path / "b", "Widget", b"{")

    # when
    found = _reader(good, broken).read(SPEC)

    # then — resolved by the readable install, and the other is told, not silently dropped
    assert found.directory == "Widget2026.3"
    assert [problem for problem in found.problems if repr(broken) in problem and "cannot be read" in problem] != []


def test_when_no_install_can_be_read_each_reason_is_listed(tmp_path) -> None:
    # given
    broken = _app(tmp_path, "Widget", _info("Widget2026.2", vendor="Other"))

    # when
    found = _reader(broken).read(SPEC)

    # then
    assert (found.directory, [problem for problem in found.problems if "vendor" in problem] != []) == (None, True)
