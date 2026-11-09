# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the system `AgentProjectIndex`: the derived folder, the unsafe roots, the overrides (issue #56).

Settings files here hold only the one key the index reads, with an invented value; the real `~/.claude` is never read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from syncintosh.core.agent_projects import AgentLayout, AgentProject, claude_project_dir_name
from syncintosh.presets.claude_memory import CLAUDE_PROJECTS
from syncintosh.system.agent_project_index import MAX_SETTINGS_BYTES, SystemAgentProjectIndex
from syncintosh.system.deny_list import SystemDenyList


def _project(name: str = "work", path: str = "code/x") -> AgentProject:
    return AgentProject.create(
        name=name, path=path, layouts=(AgentLayout.create(preset="claude-memory", spec=CLAUDE_PROJECTS),)
    )


def _read(home: Path, *projects: AgentProject, environ: dict[str, str] | None = None):
    index = SystemAgentProjectIndex(str(home), SystemDenyList(), {} if environ is None else environ)
    return index.read(CLAUDE_PROJECTS, projects)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "code" / "x").mkdir(parents=True)
    return home


def test_the_folder_is_derived_from_the_real_root(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert (entry.directory, entry.memory_relocated) == (
        claude_project_dir_name(os.path.realpath(home / "code" / "x")),
        False,
    )


def test_a_root_reached_through_a_symlinked_home_uses_the_real_path(tmp_path: Path) -> None:
    # given — the project is addressed through a link to the directory that holds it
    home = _home(tmp_path)
    link = tmp_path / "linked-home"
    link.symlink_to(home)

    # when
    entry = _read(link, _project()).lookup("work")

    # then
    assert entry.directory == claude_project_dir_name(os.path.realpath(home / "code" / "x"))


@pytest.mark.parametrize("variable", ["CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PROJECT_DIR_NAME"])
def test_an_environment_override_refuses_every_project_and_says_which(tmp_path: Path, variable: str) -> None:
    # given
    home = _home(tmp_path)

    # when
    entry = _read(home, _project(), environ={variable: "/somewhere"}).lookup("work")

    # then
    assert (entry.directory, variable in entry.reason) == (None, True)


def test_an_empty_override_is_not_set(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)

    # when
    entry = _read(home, _project(), environ={"CLAUDE_CONFIG_DIR": ""}).lookup("work")

    # then
    assert entry.directory is not None


def test_a_root_that_is_not_on_this_machine_is_unresolved(tmp_path: Path) -> None:
    # given
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert (entry.directory, "not on this machine" in entry.reason) == (None, True)


def test_a_root_that_is_a_file_is_unresolved(tmp_path: Path) -> None:
    # given
    home = tmp_path / "home"
    (home / "code").mkdir(parents=True)
    (home / "code" / "x").write_text("not a directory", encoding="utf-8")

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert entry.directory is None


def test_a_root_that_is_a_symlink_is_unresolved(tmp_path: Path) -> None:
    # given
    home = tmp_path / "home"
    (home / "code").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (home / "code" / "x").symlink_to(tmp_path / "elsewhere")

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert (entry.directory, "symbolic link" in entry.reason) == (None, True)


@pytest.mark.parametrize("root", [".ssh", ".aws", "Library/Cookies", ".claude/sessions"])
def test_a_root_on_a_dangerous_path_is_unresolved(tmp_path: Path, root: str) -> None:
    # given
    home = tmp_path / "home"
    (home / root).mkdir(parents=True)

    # when
    entry = _read(home, _project(path=root)).lookup("work")

    # then
    assert (entry.directory, "protected" in entry.reason) == (None, True)


def test_a_root_whose_parent_leaves_the_home_is_unresolved(tmp_path: Path) -> None:
    # given — `code` is a link out of the home, so `code/x` really lives elsewhere
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "outside" / "x").mkdir(parents=True)
    (home / "code").symlink_to(tmp_path / "outside")

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert (entry.directory, "leaves the home" in entry.reason) == (None, True)


@pytest.mark.parametrize(
    "where",
    [".claude/settings.json", "code/x/.claude/settings.json", "code/x/.claude/settings.local.json"],
)
def test_a_relocated_memory_directory_is_noticed_wherever_it_is_set(tmp_path: Path, where: str) -> None:
    # given
    home = _home(tmp_path)
    (home / where).parent.mkdir(parents=True, exist_ok=True)
    (home / where).write_text('{"autoMemoryDirectory": "invented-place"}', encoding="utf-8")

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert entry.memory_relocated is True


@pytest.mark.parametrize(
    "text", ['{"theme": "dark"}', '{"autoMemoryDirectory": ""}', '{"autoMemoryDirectory": null}', "[]"]
)
def test_a_settings_file_that_does_not_set_it_leaves_memory_alone(tmp_path: Path, text: str) -> None:
    # given
    home = _home(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(text, encoding="utf-8")

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert entry.memory_relocated is False


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe", b"x" * (MAX_SETTINGS_BYTES + 1)])
def test_a_settings_file_that_cannot_be_read_counts_as_relocated(tmp_path: Path, content: bytes) -> None:
    # given — "not set" would be a guess, so the memory is skipped
    home = _home(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_bytes(content)

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert entry.memory_relocated is True


def test_a_settings_file_that_is_a_symlink_counts_as_relocated(tmp_path: Path) -> None:
    # given
    home = _home(tmp_path)
    (home / ".claude").mkdir()
    (tmp_path / "real.json").write_text("{}", encoding="utf-8")
    (home / ".claude" / "settings.json").symlink_to(tmp_path / "real.json")

    # when
    entry = _read(home, _project()).lookup("work")

    # then
    assert entry.memory_relocated is True


def test_the_environment_is_read_when_the_index_reads_not_when_it_is_built(tmp_path: Path, monkeypatch) -> None:
    # given
    home = _home(tmp_path)
    index = SystemAgentProjectIndex(str(home), SystemDenyList())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/moved")

    # when
    entry = index.read(CLAUDE_PROJECTS, (_project(),)).lookup("work")

    # then
    assert entry.directory is None  # ty: ignore[unresolved-attribute]
