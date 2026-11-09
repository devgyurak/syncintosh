# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for an agent's per-project data: the folder name, the logical path, the layout, the map (issue #56).

The `claude_project_dir_name` vectors were produced by the CLI's own `J0` function run in Node (`normalize("NFC")`,
`replace(/[^a-zA-Z0-9]/g, "-")`, `slice(0, 200)` and a 32-bit `h * 31 + charCode` hash), so they check the port against
the original and not against a second copy of it.
"""

from __future__ import annotations

import pytest

from syncintosh.core.agent_projects import (
    AgentLayout,
    AgentProject,
    AgentProjectEntry,
    AgentProjectMap,
    AgentProjectSpec,
    claude_project_dir_name,
    decode_agent_project_name,
    encode_agent_project_name,
    validate_agent_project_name,
)
from syncintosh.core.errors import ConfigError

SPEC = AgentProjectSpec.create(home=".claude/projects", kind="claude", memory_prefix="memory/")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/a/src/x", "-Users-a-src-x"),
        # `.`, `_` and a space are not alphanumeric, and runs of dashes are not collapsed.
        ("/Users/a/.config/my_app x", "-Users-a--config-my-app-x"),
        ("/Users/김/x", "-Users---x"),
        # A decomposed Hangul syllable is normalized to one unit first.
        ("/한", "--"),
        ("/한", "--"),
        # A character outside the BMP is two UTF-16 units, so two dashes.
        ("/😀", "---"),
        ("/" + "a" * 199, "-" + "a" * 199),
        ("/" + "a" * 200, "-" + "a" * 199 + "-b6ymvl"),
        ("/" + "가나다/" * 80, "-" * 200 + "-44zowx"),
        ("/" + "z" * 400, "-" + "z" * 199 + "-1o8up"),
    ],
)
def test_the_project_folder_name_matches_the_cli(path: str, expected: str) -> None:
    # when
    name = claude_project_dir_name(path)

    # then
    assert name == expected


@pytest.mark.parametrize("path", ["", "relative/path", "Users/a"])
def test_a_project_path_must_be_absolute(path: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        claude_project_dir_name(path)


@pytest.mark.parametrize("name", ["work", "my project", "프로젝트", "a.b_c-d", "..", "-x"])
def test_a_plain_name_round_trips_through_its_element(name: str) -> None:
    # when
    element = encode_agent_project_name(name)

    # then
    assert (element, decode_agent_project_name(element)) == ("@" + name, name)


@pytest.mark.parametrize(
    "name",
    ["", "a/b", "@x", "a*", "a?", "é", "bad\x00", 7],
)
def test_a_name_that_could_change_the_shape_of_a_path_is_refused(name: object) -> None:
    # when & then
    with pytest.raises(ConfigError):
        validate_agent_project_name(name)  # ty: ignore[invalid-argument-type]  # a non-string is the case


@pytest.mark.parametrize("element", ["", "@", "work", "@a/b", "@a*", "@é"])
def test_an_element_that_is_not_a_canonical_logical_one_decodes_to_nothing(element: str) -> None:
    # when
    name = decode_agent_project_name(element)

    # then
    assert name is None


def test_a_logical_path_is_taken_apart_into_its_project_and_file() -> None:
    # given
    path = SPEC.logical_path("work", "memory/MEMORY.md")

    # when
    parsed = SPEC.parse(path)

    # then
    assert (path, parsed.name, parsed.relative) == (  # ty: ignore[unresolved-attribute]
        ".claude/projects/@work/memory/MEMORY.md",
        "work",
        "memory/MEMORY.md",
    )


@pytest.mark.parametrize(
    "path",
    [
        ".claude/projects/-Users-a-src-x/memory/MEMORY.md",
        ".claude/projects/@/memory/MEMORY.md",
        ".claude/projects/@work",
        ".claude/projects/@work/memory/*.md",
        ".claude/Projects/@work/memory/MEMORY.md",
        ".claude/projects/@work/../x",
        "elsewhere/@work/memory/MEMORY.md",
    ],
)
def test_a_path_that_is_not_a_logical_project_path_is_not_parsed(path: str) -> None:
    # when
    parsed = SPEC.parse(path)

    # then
    assert parsed is None


def test_the_template_of_a_logical_path_names_the_bare_marker() -> None:
    # when
    template = SPEC.template_of(SPEC.logical_path("work", "memory/a.md"))

    # then
    assert (template, SPEC.relative_of_template(template or "")) == (".claude/projects/@/memory/a.md", "memory/a.md")


@pytest.mark.parametrize(
    ("relative", "held"),
    [("memory/MEMORY.md", True), ("memory/sub/a.md", True), ("abc.jsonl", False), ("memoryx/a.md", False)],
)
def test_only_the_relocatable_part_counts_as_memory(relative: str, held: bool) -> None:
    # when
    result = SPEC.holds_memory(relative)

    # then
    assert result is held


def test_a_layout_without_a_memory_prefix_holds_no_memory() -> None:
    # given
    spec = AgentProjectSpec.create(home=".claude/projects", kind="claude")

    # when
    result = spec.holds_memory("memory/MEMORY.md")

    # then
    assert result is False


@pytest.mark.parametrize("home", ["", "/abs", "a/../b", "a//b"])
def test_a_layout_with_a_bad_home_is_refused(home: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentProjectSpec.create(home=home, kind="claude")


def test_an_unknown_agent_kind_is_refused() -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentProjectSpec.create(home=".x", kind="other")  # ty: ignore[invalid-argument-type]  # the bad kind is the case


def _layout(preset: str = "claude-memory") -> AgentLayout:
    return AgentLayout.create(preset=preset, spec=SPEC)


def test_a_project_needs_a_preset_or_nothing_would_travel() -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentProject.create(name="work", path="src/x", layouts=())


@pytest.mark.parametrize("path", ["", "/abs", "a/../b", "src/*"])
def test_a_project_root_must_be_a_plain_home_relative_path(path: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentProject.create(name="work", path=path, layouts=(_layout(),))


def test_a_preset_enabled_twice_for_one_project_is_refused() -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentProject.create(name="work", path="src/x", layouts=(_layout(), _layout()))


@pytest.mark.parametrize("preset", ["", 3])
def test_a_layout_names_its_preset(preset: object) -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentLayout.create(preset=preset, spec=SPEC)  # ty: ignore[invalid-argument-type]  # a bad name is the case


def test_a_layout_needs_a_spec() -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentLayout.create(preset="p", spec="nope")  # ty: ignore[invalid-argument-type]  # a bad spec is the case


@pytest.mark.parametrize("directory", ["", "a/b", ".", ".."])
def test_a_resolved_entry_names_one_path_element(directory: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        AgentProjectEntry.resolved(name="work", root="src/x", directory=directory)


def test_two_names_that_fold_to_one_are_refused_by_the_map() -> None:
    # given
    entries = (
        AgentProjectEntry.resolved(name="Work", root="a", directory="-a"),
        AgentProjectEntry.resolved(name="work", root="b", directory="-b"),
    )

    # when & then
    with pytest.raises(ConfigError):
        AgentProjectMap.create(entries)


def test_two_projects_on_one_directory_are_refused_by_the_map() -> None:
    # given
    entries = (
        AgentProjectEntry.resolved(name="a", root="a", directory="-same"),
        AgentProjectEntry.resolved(name="b", root="b", directory="-same"),
    )

    # when & then
    with pytest.raises(ConfigError):
        AgentProjectMap.create(entries)


def test_a_lookup_folds_the_name_and_finds_nothing_for_an_unmapped_one() -> None:
    # given
    mapped = AgentProjectMap.create((AgentProjectEntry.resolved(name="Work", root="a", directory="-a"),))

    # when
    found, missing = mapped.lookup("WORK"), mapped.lookup("other")

    # then
    assert (found and found.directory, missing) == ("-a", None)


def test_an_unresolved_entry_keeps_its_reason_and_has_no_directory() -> None:
    # when
    entry = AgentProjectEntry.unresolved(name="work", root="a", reason="not here")

    # then
    assert (entry.directory, entry.reason) == (None, "not here")
