# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the resolver's agent-project half: allowlist, catalog, resolve, describe, config file, doctor row.

The preset is the shipped `claude-memory`; the project map is a plain value, so no filesystem is touched here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.core.agent_projects import AgentLayout, AgentProject, AgentProjectEntry, AgentProjectMap
from syncintosh.core.config import SyncConfig, allows_path
from syncintosh.core.errors import ConfigError
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.claude_memory import CLAUDE_MEMORY, CLAUDE_PROJECTS
from syncintosh.services.profile_paths import (
    AgentProjectView,
    LivePaths,
    Located,
    Unresolved,
    agent_project_summary,
    describe_with,
    open_live_paths,
)
from syncintosh.services.setup import new_machine_config
from syncintosh.system.config import load_config, save_config

CATALOG = BuiltinCatalog.default()
MEMORY = ".claude/projects/@work/memory/MEMORY.md"
FACT = ".claude/projects/@work/memory/user-role.md"
FOLDER = ".claude/projects/-Users-b-code-x"


def _project(name: str = "work", path: str = "code/x") -> AgentProject:
    return AgentProject.create(
        name=name, path=path, layouts=(AgentLayout.create(preset="claude-memory", spec=CLAUDE_PROJECTS),)
    )


def _config(*projects: AgentProject) -> SyncConfig:
    return SyncConfig.create(remote="r", machine_id="m", items=(), agent_projects=projects)


def _paths(entry: AgentProjectEntry | None) -> LivePaths:
    found = AgentProjectMap.create(() if entry is None else (entry,))
    return LivePaths(catalog=CATALOG, agent_views=(AgentProjectView(spec=CLAUDE_PROJECTS, projects=found),))


def _entry(**changes: object) -> AgentProjectEntry:
    fields: dict[str, object] = {"name": "work", "root": "code/x", "directory": "-Users-b-code-x"}
    return AgentProjectEntry(**{**fields, **changes})  # ty: ignore[invalid-argument-type]  # a test builds the entry


# --------------------------------------------------------------------------- the allowlist


@pytest.mark.parametrize("path", [MEMORY, FACT])
def test_a_mapped_projects_logical_path_is_allowed(path: str) -> None:
    # when
    allowed = allows_path(_config(_project()), scope="common", machine=None, path=path)

    # then
    assert allowed is True


@pytest.mark.parametrize(
    ("path", "scope"),
    [
        (".claude/projects/@other/memory/MEMORY.md", "common"),
        (".claude/projects/-Users-b-code-x/memory/MEMORY.md", "common"),
        (MEMORY, "machine"),
    ],
)
def test_a_name_this_config_does_not_map_is_never_allowed(path: str, scope: str) -> None:
    # when
    allowed = allows_path(_config(_project()), scope=scope, machine=None, path=path)  # ty: ignore[invalid-argument-type]

    # then
    assert allowed is False


def test_no_mapping_allows_no_project_path() -> None:
    # when
    allowed = allows_path(_config(), scope="common", machine=None, path=MEMORY)

    # then
    assert allowed is False


@pytest.mark.parametrize(
    "projects",
    [(_project("work", "a"), _project("Work", "b")), (_project("a", "same"), _project("b", "same"))],
)
def test_a_name_or_a_root_may_be_mapped_once(projects: tuple[AgentProject, ...]) -> None:
    # when & then
    with pytest.raises(ConfigError):
        _config(*projects)


# --------------------------------------------------------------------------- the catalog


@pytest.mark.parametrize("path", [MEMORY, FACT])
def test_the_catalog_owns_a_logical_project_path_and_takes_the_template_policy(path: str) -> None:
    # when
    owner, policy, known = CATALOG.preset_for(path), CATALOG.policy_for(path), CATALOG.knows("claude-memory", path)

    # then
    assert (owner, policy and policy.path, known) == (CLAUDE_MEMORY, ".claude/projects/@/memory/*.md", True)


@pytest.mark.parametrize(
    "path",
    [
        ".claude/projects/-Users-b-code-x/memory/MEMORY.md",
        ".claude/projects/@/memory/MEMORY.md",
        ".claude/projects/@work/abc.jsonl",
        ".claude/projects/@work/memory/*.md",
    ],
)
def test_a_raw_folder_the_template_or_a_file_no_preset_lists_is_not_a_record_path(path: str) -> None:
    # when
    known, anonymous = CATALOG.knows("claude-memory", path), CATALOG.knows("custom", path)

    # then
    assert (known, anonymous) == (False, False)


def test_two_presets_may_share_one_project_home() -> None:
    # given
    transcripts = Preset.create(
        name="claude-sessions",
        files=(PresetFile.create(path=CLAUDE_PROJECTS.template_path("*.jsonl"), format="text"),),
        agent_projects=CLAUDE_PROJECTS,
    )
    catalog = BuiltinCatalog.create((CLAUDE_MEMORY, transcripts))

    # when
    memory, session = (
        catalog.knows("claude-memory", MEMORY),
        catalog.knows("claude-sessions", ".claude/projects/@work/abc.jsonl"),
    )

    # then
    assert (memory, session) == (True, True)


@pytest.mark.parametrize(
    "path", [".claude/other/@/memory/MEMORY.md", "memory/MEMORY.md", ".claude/projects/@work/a.md"]
)
def test_a_preset_lists_its_files_for_the_bare_marker_under_its_home(path: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="p", files=(PresetFile.create(path=path, format="text"),), agent_projects=CLAUDE_PROJECTS)


def test_an_agent_project_preset_cannot_also_use_profiles_or_an_ignore_list() -> None:
    # given
    ignored = PresetFile.create(path=CLAUDE_PROJECTS.template_path("a.json"), format="jsonc", ignore_list=True)

    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="p", files=(ignored,), agent_projects=CLAUDE_PROJECTS)


# --------------------------------------------------------------------------- resolve


def test_a_logical_path_resolves_to_the_folder_this_machine_derived() -> None:
    # when
    located = _paths(_entry()).resolve(MEMORY)

    # then
    assert located == Located(f"{FOLDER}/memory/MEMORY.md", agent_directory=FOLDER, agent_root="code/x")


def test_the_same_record_resolves_to_each_machines_own_folder() -> None:
    # when
    first = _paths(_entry(root="src/x", directory="-Users-a-src-x")).resolve(MEMORY)
    second = _paths(_entry()).resolve(MEMORY)

    # then
    assert (first.path, second.path) == (  # ty: ignore[unresolved-attribute]
        ".claude/projects/-Users-a-src-x/memory/MEMORY.md",
        f"{FOLDER}/memory/MEMORY.md",
    )


def test_a_name_the_config_does_not_map_resolves_to_nothing() -> None:
    # when
    result = _paths(None).resolve(MEMORY)

    # then
    assert isinstance(result, Unresolved) and "agent_projects" in result.reason


def test_a_project_with_no_folder_here_resolves_to_its_reason() -> None:
    # when
    result = _paths(AgentProjectEntry.unresolved(name="work", root="code/x", reason="not cloned")).resolve(MEMORY)

    # then
    assert result == Unresolved("not cloned")


def test_a_relocated_memory_directory_is_left_alone() -> None:
    # when
    result = _paths(_entry(memory_relocated=True)).resolve(MEMORY)

    # then
    assert isinstance(result, Unresolved) and "autoMemoryDirectory" in result.reason


def test_a_layout_this_machine_did_not_read_resolves_to_nothing() -> None:
    # when
    result = LivePaths(catalog=CATALOG).resolve(MEMORY)

    # then
    assert isinstance(result, Unresolved)


@pytest.mark.parametrize(
    "path", [".claude/projects/-Users-b-code-x/memory/MEMORY.md", ".claude/projects/@/memory/a.md"]
)
def test_a_raw_folder_or_the_template_never_resolves(path: str) -> None:
    # when
    result = _paths(_entry()).resolve(path)

    # then
    assert isinstance(result, Unresolved)


def test_a_glob_is_resolved_through_its_directory() -> None:
    # when
    located = _paths(_entry()).resolve_project(CLAUDE_PROJECTS, "work", "memory/*.md")

    # then
    assert located.path.rpartition("/")[0] == f"{FOLDER}/memory"  # ty: ignore[unresolved-attribute]


def test_a_path_is_described_by_its_project_name_and_never_its_real_folder() -> None:
    # when
    text = describe_with(CATALOG, FACT)

    # then
    assert text == 'claude-memory project "work": memory/user-role.md'


# --------------------------------------------------------------------------- the index is read once, per layout


class _Index:
    def __init__(self) -> None:
        self.reads: list[tuple[object, tuple[str, ...]]] = []

    def read(self, spec, projects):
        self.reads.append((spec, tuple(each.name for each in projects)))
        return AgentProjectMap.create(())


def test_each_layout_is_read_once_with_the_projects_that_enable_it() -> None:
    # given
    index = _Index()

    # when
    paths = open_live_paths(_config(_project("a", "x"), _project("b", "y")), CATALOG, None, None, index)

    # then
    assert (index.reads, len(paths.agent_views)) == ([(CLAUDE_PROJECTS, ("a", "b"))], 1)


def test_no_index_reads_nothing() -> None:
    # when
    paths = open_live_paths(_config(_project()), CATALOG, None)

    # then
    assert paths.agent_views == ()


# --------------------------------------------------------------------------- doctor


def test_the_doctor_row_is_absent_when_no_project_is_mapped() -> None:
    # when
    row = agent_project_summary(_config(), _paths(None))

    # then
    assert row is None


def test_the_doctor_row_names_each_project_and_why_one_is_not_ready() -> None:
    # given
    found = AgentProjectMap.create(
        (
            _entry(),
            AgentProjectEntry.unresolved(name="other", root="o", reason="CLAUDE_CONFIG_DIR is set"),
        )
    )
    paths = LivePaths(catalog=CATALOG, agent_views=(AgentProjectView(spec=CLAUDE_PROJECTS, projects=found),))

    # when
    row = agent_project_summary(_config(_project(), _project("other", "o")), paths)

    # then
    assert (row.ok, row.detail) == (  # ty: ignore[unresolved-attribute]
        True,
        '"work": ready; "other": CLAUDE_CONFIG_DIR is set; its records are kept as they are',
    )


# --------------------------------------------------------------------------- the config file


def test_a_mapped_project_round_trips_through_the_config_file(tmp_path: Path) -> None:
    # given
    config = _config(_project())
    file = tmp_path / "config.json"

    # when
    save_config(config, file)
    loaded = load_config(file)

    # then
    assert loaded.agent_projects == config.agent_projects


def test_a_config_that_maps_nothing_keeps_its_exact_shape(tmp_path: Path) -> None:
    # given
    file = tmp_path / "config.json"

    # when
    save_config(_config(), file)

    # then
    assert "agent_projects" not in file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "entry",
    [
        '{"name": "w", "path": "x"}',
        '{"name": "w", "path": "x", "presets": ["zsh"]}',
        '{"name": "w", "path": "x", "presets": ["nope"]}',
        '{"name": "w", "path": "x", "presets": "claude-memory"}',
        '{"name": "w", "path": "x", "presets": ["claude-memory"], "extra": 1}',
        '{"name": "w/x", "path": "x", "presets": ["claude-memory"]}',
        '"not an object"',
    ],
)
def test_a_bad_agent_project_entry_is_refused_at_load(tmp_path: Path, entry: str) -> None:
    # given
    file = tmp_path / "config.json"
    file.write_text(f'{{"remote": "r", "machine_id": "m", "items": [], "agent_projects": [{entry}]}}', encoding="utf-8")

    # when & then
    with pytest.raises(ConfigError):
        load_config(file)


def test_agent_projects_must_be_a_list(tmp_path: Path) -> None:
    # given
    file = tmp_path / "config.json"
    file.write_text('{"remote": "r", "machine_id": "m", "items": [], "agent_projects": {}}', encoding="utf-8")

    # when & then
    with pytest.raises(ConfigError):
        load_config(file)


def test_init_refuses_a_per_project_preset_instead_of_tracking_nothing() -> None:
    # when & then
    with pytest.raises(ConfigError, match="agent_projects"):
        new_machine_config(remote="r", machine_id="m", preset_names=("claude-memory",), catalog=CATALOG)
