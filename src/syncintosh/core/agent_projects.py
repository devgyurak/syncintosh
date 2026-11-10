# SPDX-License-Identifier: AGPL-3.0-or-later
"""A coding agent's **per-project** data, addressed by the project's **name**: the logical path, the layout, the map.

Claude Code keeps a project's memory and transcripts under `~/.claude/projects/<dir>/`, and `<dir>` is the project's
absolute path spelled with every non-alphanumeric character turned into `-` (`J0` in the CLI). Two Macs rarely share a
username and a clone location, so a record keyed by the real directory would never match across machines (issue #56).
A record instead carries a **logical path** that names the project:

```
.claude/projects/@<name>/memory/MEMORY.md
```

* `<name>` is a label the person chose in this machine's own config (`agent_projects`), never a path. The leading ``@``
  marks the element as logical, so a name such as ``..`` cannot be read as a path element, and a real directory (which
  `claude_project_dir_name` spells without ``@``) can never share a record's path (the same device as a VS Code profile,
  `core/profiles.py`).
* The manifest schema is unchanged. A build that predates this owns no such path, so it refuses the record at the
  catalog check — the fail-closed direction.
* Which real directory a name stands for is **local**: the config maps the name to this machine's project root, and the
  directory is derived from that root. A name the config does not map is never written (decision D3).

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import struct
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import validate_path
from syncintosh.core.paths import collision_key, duplicate_paths

# The marker that sets a logical project element apart from a real directory, which is spelled without it.
AGENT_PROJECT_MARK: Final = "@"

# What stands for the project's element in a preset's own file paths (`.claude/projects/@/memory/*.md`). A bare marker
# is never a canonical logical element (it names nothing), so a record that arrives carrying the template is refused.
AGENT_PROJECT_TEMPLATE: Final = "@"

# Which agent's directory naming a layout uses. A `Literal`, not an `Enum`: it is compared, never serialized.
AgentKind = Literal["claude", "codex"]

# The CLI cuts a longer name to this many characters and appends a hash of the path (`claude_project_dir_name`).
_NAME_LIMIT: Final = 200
_GLOB_CHARACTERS: Final = ("*", "?")
_HASH_BASE: Final = 36
_DIGITS: Final = "0123456789abcdefghijklmnopqrstuvwxyz"


def _utf16_units(text: str) -> tuple[int, ...]:
    """The UTF-16 code units of `text`: the unit the CLI's string functions (`replace`, `charCodeAt`) work in."""
    data = text.encode("utf-16-le", "surrogatepass")
    return struct.unpack(f"<{len(data) // 2}H", data)


def _base36(number: int) -> str:
    """`number.toString(36)` for a non-negative integer."""
    digits: list[str] = []
    while True:
        number, remainder = divmod(number, _HASH_BASE)
        digits.append(_DIGITS[remainder])
        if number == 0:
            return "".join(reversed(digits))


def claude_project_dir_name(path: str) -> str:
    """The folder name Claude Code files a project's data under, from the project's **real** absolute path.

    Taken from the CLI's own function (2.1.308, `J0`): NFC, then every character outside `[a-zA-Z0-9]` becomes `-`
    (runs are not collapsed). The replace works on UTF-16 code units, so a character outside the BMP becomes two
    dashes. A name over 200 characters is cut to 200 and gets `-` and the base-36 absolute value of a 32-bit
    `h * 31 + unit` hash of the path. The caller resolves symlinks first (`realpath`): this is a pure function.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise ConfigError(f"a project path must be absolute, got {path!r}")
    units = _utf16_units(unicodedata.normalize("NFC", path))
    name = "".join(chr(unit) if chr(unit).isascii() and chr(unit).isalnum() else "-" for unit in units)
    if len(name) <= _NAME_LIMIT:
        return name
    digest = 0
    for unit in units:
        digest = (digest * 31 + unit) & 0xFFFFFFFF
    signed = digest - 0x100000000 if digest >= 0x80000000 else digest
    return f"{name[:_NAME_LIMIT]}-{_base36(abs(signed))}"


def validate_agent_project_name(name: str) -> str:
    """The name, or `ConfigError` when it cannot be one path element after the marker.

    Non-empty, already NFC (two spellings of one name would be two records for one directory), and free of the
    characters that would change a path's shape: `/`, the marker, and a glob character. The manifest's own path rule
    runs on the finished element, so a control character or a surrounding space is refused too.
    """
    if not isinstance(name, str) or name == "":
        raise ConfigError(f"a project name must be a non-empty string, got {name!r}")
    if name != unicodedata.normalize("NFC", name):
        raise ConfigError(f"a project name must be NFC-normalized text, got {name!r}")
    if "/" in name or AGENT_PROJECT_MARK in name or any(character in name for character in _GLOB_CHARACTERS):
        raise ConfigError(f"a project name must not hold '/', '@', '*' or '?': {name!r}")
    try:
        validate_path(AGENT_PROJECT_MARK + name)
    except ManifestCorrupt as exc:
        raise ConfigError(f"invalid project name {name!r}: {exc}") from exc
    return name


def encode_agent_project_name(name: str) -> str:
    """The path element a project name becomes, marker included (`@work`)."""
    return AGENT_PROJECT_MARK + validate_agent_project_name(name)


def decode_agent_project_name(element: str) -> str | None:
    """The name a path element encodes, or `None` when it is not a well-formed logical element."""
    if not element.startswith(AGENT_PROJECT_MARK) or len(element) == 1:
        return None
    name = element[1:]
    try:
        validate_agent_project_name(name)
    except ConfigError:
        return None
    return name


def agent_project_key(name: str) -> str:
    """The identity under which two project names are one project (`collision_key`)."""
    return collision_key(name)


@dataclass(frozen=True)
class AgentProjectPath:
    """A logical path taken apart: which project, and the file's place inside the project's directory."""

    name: str
    relative: str


@dataclass(frozen=True)
class AgentProjectSpec:
    """How one agent lays a project's data out, as data.

    * `home` — the directory that holds one folder per project (`.claude/projects`). The logical path and the real
      path share this prefix.
    * `kind` — which agent's layout this is. Claude Code keeps a project's data in a folder named after the project's
      real path (`claude_project_dir_name`); Codex keeps every session in one date tree and names the project only
      inside the session's first record, so its layout is `flat`: the real directory is `home` itself.
    * `memory_prefix` — the part of a project's folder the agent lets the person relocate (`autoMemoryDirectory`),
      or `None`. A file under it is skipped for a project that relocated it (decision D3).
    """

    home: str
    kind: AgentKind
    memory_prefix: str | None = None

    @classmethod
    def create(cls, *, home: str, kind: AgentKind, memory_prefix: str | None = None) -> AgentProjectSpec:
        """Validate and freeze the layout; never assign fields afterwards."""
        try:
            validated_home = validate_path(home)
            validated_prefix = None if memory_prefix is None else validate_path(memory_prefix.rstrip("/")) + "/"
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid agent project layout: {exc}") from exc
        if kind not in ("claude", "codex"):
            raise ConfigError(f"unknown agent project kind: {kind!r}")
        return cls(home=validated_home, kind=kind, memory_prefix=validated_prefix)

    def logical_path(self, name: str, relative: str) -> str:
        """The record path for `relative` inside the project called `name`."""
        return f"{self.home}/{encode_agent_project_name(name)}/{validate_path(relative)}"

    def template_path(self, relative: str) -> str:
        """The path a preset lists for `relative`: the project's element is the bare marker."""
        return f"{self.home}/{AGENT_PROJECT_TEMPLATE}/{validate_path(relative)}"

    def parse(self, path: str) -> AgentProjectPath | None:
        """Take a logical path apart, or `None` when `path` is not a well-formed one of this layout.

        Well-formed means: under `home`, a canonical `@<name>` element, and a valid relative path below it. The home
        is matched exactly, not folded: a logical path is only ever written by this code, so a differently cased
        spelling is not ours, and accepting it would let a record choose the case `apply` creates.
        """
        parts, home = path.split("/"), self.home.split("/")
        if len(parts) < len(home) + 2 or parts[: len(home)] != home:
            return None
        name = decode_agent_project_name(parts[len(home)])
        relative = "/".join(parts[len(home) + 1 :])
        if name is None or relative == "":
            return None
        try:
            validate_path(relative)
        except ManifestCorrupt:
            return None
        if any(character in relative for character in _GLOB_CHARACTERS):
            # A pattern is a preset's text, never a record's path: `apply` would create a file called `*.md`.
            return None
        return AgentProjectPath(name=name, relative=relative)

    def template_of(self, path: str) -> str | None:
        """The preset-side path (with the bare marker) a logical `path` takes its policy from, or `None`."""
        parsed = self.parse(path)
        return None if parsed is None else self.template_path(parsed.relative)

    def relative_of_template(self, template: str) -> str | None:
        """The part of a preset's template path below the project's element, or `None` when it is not one."""
        prefix = f"{self.home}/{AGENT_PROJECT_TEMPLATE}/"
        return template[len(prefix) :] if template.startswith(prefix) and len(template) > len(prefix) else None

    @property
    def flat(self) -> bool:
        """True when the agent has no per-project folder: a project's files sit in `home`'s own tree."""
        return self.kind == "codex"

    def holds_memory(self, relative: str) -> bool:
        """True when `relative` lies in the part the agent lets the person relocate."""
        return self.memory_prefix is not None and relative.startswith(self.memory_prefix)

    def contains_home(self, path: str) -> bool:
        """True when `path` lies under this layout's home, whatever it names (a raw folder or a logical one)."""
        parts, home = (
            [collision_key(part) for part in path.split("/")],
            [collision_key(p) for p in self.home.split("/")],
        )
        return len(parts) > len(home) and parts[: len(home)] == home


@dataclass(frozen=True)
class AgentLayout:
    """One preset's layout, as this machine's config enabled it for a project."""

    preset: str
    spec: AgentProjectSpec

    @classmethod
    def create(cls, *, preset: str, spec: AgentProjectSpec) -> AgentLayout:
        """Validate and freeze one layout; the preset name is checked against the catalog by the caller."""
        if not isinstance(preset, str) or preset == "":
            raise ConfigError(f"a layout must name its preset, got {preset!r}")
        if not isinstance(spec, AgentProjectSpec):
            raise ConfigError(f"a layout needs a layout spec, got {type(spec).__name__}")
        return cls(preset=preset, spec=spec)


@dataclass(frozen=True)
class AgentProject:
    """One project this machine opted in: its name, where its root is on this Mac, and which presets travel.

    `path` is `$HOME`-relative (`Develop/syncintosh`), validated like a tracked path. It is the only place the real
    project is named, and it is never synced: another machine maps the same `name` to its own root. `layouts` are the
    presets this machine enabled for the project; none is enabled by default (decision D5).
    """

    name: str
    path: str
    layouts: tuple[AgentLayout, ...]

    @classmethod
    def create(cls, *, name: str, path: str, layouts: Iterable[AgentLayout]) -> AgentProject:
        """Validate the name, the root and the layouts, and freeze."""
        validated_name = validate_agent_project_name(name)
        try:
            validated_path = validate_path(path)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid project path: {exc}") from exc
        if any(character in validated_path for character in _GLOB_CHARACTERS):
            raise ConfigError(f"a project path must not hold a glob character: {validated_path!r}")
        chosen = tuple(layouts)
        if not chosen:
            raise ConfigError(f"project {validated_name!r} enables no preset, so nothing would travel")
        repeated = duplicate_paths(layout.preset for layout in chosen)
        if repeated:
            raise ConfigError(f"project {validated_name!r} enables a preset twice: {list(repeated)}")
        return cls(name=validated_name, path=validated_path, layouts=chosen)


def duplicate_agent_project_names(names: Iterable[str]) -> tuple[str, ...]:
    """The folded names that appear more than once, sorted (`duplicate_paths` is the same rule for paths)."""
    keys = [agent_project_key(name) for name in names]
    return tuple(sorted({key for key in keys if keys.count(key) > 1}))


@dataclass(frozen=True)
class AgentProjectEntry:
    """One project as this machine really has it: the folder its data lives in, or why there is none.

    `directory` is the single folder name under `home` (`claude_project_dir_name` of the real root), or `None` with the
    sentence in `reason`. `memory_relocated` is `True` when the agent was told to keep the project's memory somewhere
    else, so the files under `memory_prefix` are not where this layout looks for them.
    """

    name: str
    root: str
    directory: str | None
    reason: str = ""
    memory_relocated: bool = False
    real_root: str = ""

    @classmethod
    def resolved(
        cls, *, name: str, root: str, directory: str, memory_relocated: bool = False, real_root: str = ""
    ) -> AgentProjectEntry:
        """The entry for a project whose real folder is `directory`; `real_root` is the project's resolved path."""
        if not isinstance(directory, str) or directory == "" or "/" in directory or directory in (".", ".."):
            # One element by construction, so a name derived from a path can only point inside `home`.
            raise ConfigError(f"a project directory must be one path element, got {directory!r}")
        return cls(name=name, root=root, directory=directory, memory_relocated=memory_relocated, real_root=real_root)

    @classmethod
    def unresolved(cls, *, name: str, root: str, reason: str) -> AgentProjectEntry:
        """The entry for a project with no usable folder here: nothing is read or written for it."""
        return cls(name=name, root=root, directory=None, reason=reason)


@dataclass(frozen=True)
class AgentProjectMap:
    """The projects this machine maps, by name, read once per cycle.

    A project that is not resolved is never a deletion: the caller keeps its records and defers
    (``docs/design/03-manifest.md``).
    """

    entries: tuple[AgentProjectEntry, ...]
    problems: tuple[str, ...] = ()

    @classmethod
    def create(cls, entries: tuple[AgentProjectEntry, ...], problems: tuple[str, ...] = ()) -> AgentProjectMap:
        """Freeze a map; refuse two names that are one project, or two projects on one directory."""
        repeated = duplicate_agent_project_names(entry.name for entry in entries)
        if repeated:
            raise ConfigError(f"two projects share one name: {list(repeated)}")
        shared = duplicate_paths(entry.directory for entry in entries if entry.directory is not None)
        if shared:
            # Two names on one directory would write the same files twice, and which write wins would depend on order.
            raise ConfigError(f"two projects share one directory: {list(shared)}")
        return cls(entries=tuple(entries), problems=tuple(problems))

    def lookup(self, name: str) -> AgentProjectEntry | None:
        """The entry whose name is `name` under `collision_key`, or `None`."""
        key = agent_project_key(name)
        return next((entry for entry in self.entries if agent_project_key(entry.name) == key), None)


__all__ = [
    "AGENT_PROJECT_MARK",
    "AGENT_PROJECT_TEMPLATE",
    "AgentKind",
    "AgentLayout",
    "AgentProject",
    "AgentProjectEntry",
    "AgentProjectMap",
    "AgentProjectPath",
    "AgentProjectSpec",
    "agent_project_key",
    "claude_project_dir_name",
    "decode_agent_project_name",
    "duplicate_agent_project_names",
    "encode_agent_project_name",
    "validate_agent_project_name",
]
