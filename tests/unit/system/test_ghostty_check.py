# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Ghostty config validator behind the `SyntaxValidator` port.

CI has no Ghostty and a real app must never be run: every case drives a fake `ghostty` shell script.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from syncintosh.core.errors import SyntaxInvalid
from syncintosh.system import ghostty_check
from syncintosh.system.ghostty_check import GhosttySyntaxValidator

SUBJECT = ".config/ghostty/config"
VALID = "font-size = 13\nkeybind = a=new_tab\n"
INVALID = "font-size = MARKER-abc123\n"

# The fake follows the real checker's observable contract: rc 1 for an invalid file, a value echoed on
# stderr, a required relative include resolved against the checked file's own directory (`?x` optional).
_SCRIPT = """#!/bin/sh
file="${{2#--config-file=}}"
dir="$(dirname "$file")"
echo "$file" >> "{log}"
echo "HOME=$HOME" >> "{log}"
cp "$file" "{seen}"
cat "$file" >&2
if grep -q INVALID "$file"; then exit 1; fi
if grep -q 'MARKER' "$file"; then exit 1; fi
if grep -q '^config-file = needed' "$file" && [ ! -e "$dir/needed" ]; then exit 1; fi
exit 0
"""


def _fake_app(root: Path, *, script: str | None = None, home: str = "home") -> tuple[Path, Path, Path]:
    """A `Ghostty.app` bundle in `<home>/Applications` whose binary is the fake; returns (app, call log, text copy)."""
    app = root / home / "Applications" / "Ghostty.app"
    binary = app / "Contents" / "MacOS" / "ghostty"
    binary.parent.mkdir(parents=True)
    log, seen = root / "calls.log", root / "seen.txt"
    binary.write_text(script or _SCRIPT.format(log=log, seen=seen), encoding="utf-8")
    binary.chmod(0o755)
    return app, log, seen


def _validator(app: Path, home: Path) -> GhosttySyntaxValidator:
    return GhosttySyntaxValidator(home=str(home), find=lambda bundle_id: (str(app),), timeout=60)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "ghostty").mkdir(parents=True)
    return home


def test_a_valid_config_passes(tmp_path) -> None:
    # given
    app, _, _ = _fake_app(tmp_path)

    # when & then
    _validator(app, _home(tmp_path)).check(VALID, subject=SUBJECT)


def test_an_invalid_config_is_refused(tmp_path) -> None:
    # given
    app, _, _ = _fake_app(tmp_path)

    # when & then
    with pytest.raises(SyntaxInvalid):
        _validator(app, _home(tmp_path)).check("INVALID\n", subject=SUBJECT)


@pytest.mark.parametrize(
    "found",
    [None, (), ("/nonexistent/Ghostty.app",)],
    ids=["cannot-ask", "not-installed", "no-binary-in-bundle"],
)
def test_no_binary_is_a_refusal_that_says_so(tmp_path, found) -> None:
    # given
    validator = GhosttySyntaxValidator(home=str(_home(tmp_path)), find=lambda bundle_id: found)

    # when & then — fail closed, and the message is not the one a syntax error gets
    with pytest.raises(SyntaxInvalid, match="no ghostty to validate against"):
        validator.check(VALID, subject=SUBJECT)


def test_the_bundle_id_is_asked_for_not_the_path(tmp_path) -> None:
    # given
    asked: list[str] = []

    def _find(bundle_id: str) -> tuple[str, ...] | None:
        asked.append(bundle_id)
        return None

    # when
    with pytest.raises(SyntaxInvalid):
        GhosttySyntaxValidator(home=str(_home(tmp_path)), find=_find).check(VALID, subject=SUBJECT)

    # then
    assert asked == ["com.mitchellh.ghostty"]


def test_a_binary_that_is_not_executable_is_a_refusal(tmp_path) -> None:
    # given
    app, _, _ = _fake_app(tmp_path)
    binary = app / "Contents" / "MacOS" / "ghostty"
    binary.chmod(binary.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    # when & then
    with pytest.raises(SyntaxInvalid, match="no ghostty to validate against"):
        _validator(app, _home(tmp_path)).check(VALID, subject=SUBJECT)


def test_a_binary_that_cannot_be_started_is_a_refusal(tmp_path) -> None:
    # given — executable bit set, but not a program the kernel can run
    app, _, _ = _fake_app(tmp_path, script="not a program\n")

    # when & then
    with pytest.raises(SyntaxInvalid, match="could not run"):
        _validator(app, _home(tmp_path)).check(VALID, subject=SUBJECT)


def test_a_check_that_hangs_is_a_refusal(tmp_path, monkeypatch) -> None:
    # given
    app, _, _ = _fake_app(tmp_path)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ghostty", timeout=1)

    monkeypatch.setattr("syncintosh.system.ghostty_check.subprocess.run", _timeout)

    # when & then
    with pytest.raises(SyntaxInvalid, match="could not run"):
        _validator(app, _home(tmp_path)).check(VALID, subject=SUBJECT)


@pytest.mark.parametrize("text", [VALID, "INVALID\n"], ids=["valid", "invalid"])
def test_the_temp_file_is_beside_the_target_and_gone_afterwards(tmp_path, text) -> None:
    # given
    app, log, _ = _fake_app(tmp_path)
    home = _home(tmp_path)
    directory = home / ".config" / "ghostty"

    # when
    with contextlib.suppress(SyntaxInvalid):
        _validator(app, home).check(text, subject=SUBJECT)

    # then — checked in the target's directory (so includes resolve), and removed even on failure
    checked = Path(log.read_text(encoding="utf-8").splitlines()[0])
    assert checked.parent == directory
    assert sorted(each.name for each in directory.iterdir()) == []


def test_the_temp_file_is_private_and_hidden(tmp_path) -> None:
    # given — a fake that records the mode of the file it was asked to check
    mode_log = tmp_path / "mode.log"
    script = f"""#!/bin/sh
file="${{2#--config-file=}}"
ls -l "$file" | cut -c1-10 > "{mode_log}"
basename "$file" >> "{mode_log}"
exit 0
"""
    app, _, _ = _fake_app(tmp_path, script=script)

    # when
    _validator(app, _home(tmp_path)).check(VALID, subject=SUBJECT)

    # then
    mode, name = mode_log.read_text(encoding="utf-8").split()
    assert (mode, name.startswith(".")) == ("-rw-------", True)


def test_a_missing_directory_falls_back_to_a_private_one_that_is_removed(tmp_path) -> None:
    # given — the target's directory does not exist yet (a first apply)
    app, log, _ = _fake_app(tmp_path, home="bare-home")
    home = tmp_path / "bare-home"

    # when
    _validator(app, home).check(VALID, subject=SUBJECT)

    # then — checked somewhere else, that place is gone, and no directory was made under the home
    checked = Path(log.read_text(encoding="utf-8").splitlines()[0])
    assert (checked.parent.exists(), checked.parent.parent != home, [each.name for each in home.iterdir()]) == (
        False,
        True,
        ["Applications"],
    )


@pytest.mark.parametrize(
    ("includes", "expectation"),
    [([], pytest.raises(SyntaxInvalid)), (["needed"], contextlib.nullcontext())],
    ids=["include-missing", "include-beside-target"],
)
def test_a_relative_include_is_resolved_beside_the_target(tmp_path, includes, expectation) -> None:
    # given — a required include, present in the target's directory or not
    app, _, _ = _fake_app(tmp_path)
    home = _home(tmp_path)
    [(home / ".config" / "ghostty" / name).write_text("", encoding="utf-8") for name in includes]

    # when & then
    with expectation:
        _validator(app, home).check("config-file = needed\n", subject=SUBJECT)


@pytest.mark.parametrize("place", ["Downloads", "Volumes/Ghostty-Installer", "build"])
def test_a_bundle_outside_an_install_location_is_never_run(tmp_path, place) -> None:
    # given — Spotlight lists a bundle that is not where an install puts one
    elsewhere = tmp_path / place
    app, log, _ = _fake_app(tmp_path)
    moved = elsewhere / "Ghostty.app"
    moved.parent.mkdir(parents=True)
    app.rename(moved)

    # when
    with pytest.raises(SyntaxInvalid, match="no ghostty to validate against"):
        _validator(moved, _home(tmp_path)).check(VALID, subject=SUBJECT)

    # then — and it was never started
    assert log.exists() is False


def test_a_write_that_fails_part_way_leaves_no_fragment_behind(tmp_path, monkeypatch) -> None:
    # given — the disk fills up after the file is created
    app, _, _ = _fake_app(tmp_path)
    home = _home(tmp_path)

    def _full(descriptor: int, data: bytes) -> None:
        os.close(descriptor)
        raise OSError("no space left on device")

    monkeypatch.setattr(ghostty_check, "_write_all", _full)

    # when
    with pytest.raises(SyntaxInvalid, match="could not run"):
        _validator(app, home).check(VALID, subject=SUBJECT)

    # then
    assert list((home / ".config" / "ghostty").iterdir()) == []


def test_an_optional_include_may_be_absent(tmp_path) -> None:
    # given
    app, _, _ = _fake_app(tmp_path)

    # when & then — `?` makes a machine-local include optional, so a machine without it still validates
    _validator(app, _home(tmp_path)).check("config-file = ?needed\n", subject=SUBJECT)


def test_the_checker_output_never_reaches_the_refusal(tmp_path) -> None:
    # given — the fake echoes the whole file on stderr, as the real checker echoes a value
    app, _, _ = _fake_app(tmp_path)

    # when
    with pytest.raises(SyntaxInvalid) as refusal:
        _validator(app, _home(tmp_path)).check(INVALID, subject=SUBJECT)

    # then
    assert (SUBJECT in str(refusal.value), "MARKER-abc123" in str(refusal.value)) == (True, False)


def test_the_text_reaches_the_checker_unchanged(tmp_path) -> None:
    # given
    app, _, seen = _fake_app(tmp_path)

    # when
    _validator(app, _home(tmp_path)).check(VALID, subject=SUBJECT)

    # then
    assert seen.read_text(encoding="utf-8") == VALID


def test_the_checker_gets_the_real_home_and_nothing_else_of_the_environment(tmp_path, monkeypatch) -> None:
    # given
    app, log, _ = _fake_app(tmp_path)
    home = _home(tmp_path)
    monkeypatch.setenv("SYNCINTOSH_LEAK", "x")

    # when
    _validator(app, home).check(VALID, subject=SUBJECT)

    # then — `theme = X` is looked up under $HOME, so HOME is passed; the rest is not
    assert log.read_text(encoding="utf-8").splitlines()[1] == f"HOME={home}"


def test_a_logical_tilde_subject_is_expanded_under_the_home(tmp_path) -> None:
    # given
    app, log, _ = _fake_app(tmp_path)
    home = _home(tmp_path)

    # when
    _validator(app, home).check(VALID, subject=f"~/{SUBJECT}")

    # then
    assert Path(log.read_text(encoding="utf-8").splitlines()[0]).parent == home / ".config" / "ghostty"


def test_building_the_validator_does_no_io(monkeypatch) -> None:
    # given
    def _boom(*args, **kwargs):
        raise AssertionError("constructor touched the outside world")

    monkeypatch.setattr(ghostty_check, "find_app_paths", _boom)
    monkeypatch.setattr("syncintosh.system.ghostty_check.subprocess.run", _boom)
    monkeypatch.setattr(Path, "home", _boom)

    # when & then
    GhosttySyntaxValidator()
