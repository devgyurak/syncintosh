# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profiles of one app, addressed by **name**: the logical path, the spec, and the resolved map.

VS Code keeps a non-default profile under `User/profiles/<id>/`, and `<id>` is a random int32 hash
that two Macs do not share, so a record keyed by the real path would never match across machines
(issue #22). A record instead carries a **logical path** that names the profile:

```
Library/Application Support/Code/User/profiles/@<percent-encoded name>/<file>
```

* The name is encoded with ``quote(name, safe="")``, so ``/`` ``*`` ``?`` ``@`` and ``%`` can never
  change the shape of the path, and the leading ``@`` keeps a name such as ``..`` from being read as
  a path element. A real profile id never contains ``@`` (`PROFILE_ID`), so a logical path cannot
  collide with a real one.
* The manifest schema is unchanged. A build that predates this refuses the path at the catalog check
  (no preset owns it), which is the fail-closed direction.
* A raw id path (`…/profiles/-3f2a1b/settings.json`) is owned by **no** preset, so `knows`, `policy_for`
  and the allowlist all refuse it — a remote id is never an allowlist.

Resolving a logical path to a real directory is a separate, local step: it needs the machine's own
profile index (`ProfileIndex`), which `core` only declares. Until a path is resolved, nothing may be
read or written under it (`services/sync.py`).

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote, unquote

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import validate_path
from syncintosh.core.paths import collision_key, duplicate_paths

# The marker that sets a logical profile element apart from a real one. VS Code ids are
# `hash(uuid).toString(16)`, so they never hold it.
PROFILE_MARK: Final = "@"

# One directory name under `profiles/`, as VS Code mints it: a signed int32 in hex, so a leading `-` is
# real. The built-in Agents profile is `builtin/agents` — two elements — and so does not match.
PROFILE_ID: Final = re.compile(r"-?[0-9a-f]+")


def encode_profile_name(name: str) -> str:
    """The path element a profile name becomes, marker included (`@Work`, `@a%2Fb`).

    The name is NFC-normalized first, so the decomposed and composed spellings of one name share an element.
    `decode_profile_name` re-encodes and compares, so an element written from a decomposed name is not canonical
    and is refused.
    """
    if not isinstance(name, str) or name == "":
        raise ConfigError(f"a profile name must be a non-empty string, got {name!r}")
    try:
        # NFC first: macOS treats two spellings of a name as one, and `tracking_key` cannot fold what sits
        # inside a percent escape, so two spellings would otherwise become two records for one directory.
        return PROFILE_MARK + quote(unicodedata.normalize("NFC", name), safe="", errors="strict")
    except UnicodeEncodeError as exc:
        raise ConfigError("a profile name must be valid UTF-8 text") from exc


def decode_profile_name(element: str) -> str | None:
    """The name a path element encodes, or `None` when it is not a canonical encoding.

    Canonical means `encode_profile_name` would write the same element back, so two spellings of
    one name (`%2f` against `%2F`, an unneeded escape) cannot become two records.
    """
    if not element.startswith(PROFILE_MARK) or len(element) == 1:
        return None
    try:
        name = unquote(element[1:], errors="strict")
    except UnicodeDecodeError:
        return None
    if name == "" or encode_profile_name(name) != element:
        return None
    return name


def profile_name_key(name: str) -> str:
    """The identity under which two profile names are one profile (`collision_key`)."""
    return collision_key(name)


def duplicate_profile_names(names: Iterable[str]) -> tuple[str, ...]:
    """The folded names that appear more than once, sorted.

    Names that differ only in case or Unicode normalization are one profile to a person and to the
    filesystem, and a record per spelling would write one directory twice, so a collect reports them
    instead (`duplicate_paths` is the same rule for paths).
    """
    keys = [profile_name_key(name) for name in names]
    return tuple(sorted({key for key in keys if keys.count(key) > 1}))


@dataclass(frozen=True)
class ProfileFile:
    """One file a profile may hold: its path under the profile, and the resource that decides borrowing.

    `resource` is VS Code's name for what a `useDefaultFlags` entry switches (`settings`,
    `keybindings`, `snippets`): a profile that borrows a resource reads the default profile's file,
    so its own copy is never collected or written (decision D6).
    """

    path: str
    resource: str

    @classmethod
    def create(cls, *, path: str, resource: str) -> ProfileFile:
        """Validate and freeze one profile file."""
        try:
            validated = validate_path(path)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid profile file: {exc}") from exc
        if not isinstance(resource, str) or resource == "":
            raise ConfigError(f"{validated}: a profile file must name its resource")
        return cls(path=validated, resource=resource)


@dataclass(frozen=True)
class ProfilePath:
    """A logical path taken apart: which profile, which file, and where the default profile keeps it."""

    name: str
    relative: str
    resource: str
    default_path: str


@dataclass(frozen=True)
class ProfileSpec:
    """How one app lays out its profiles, as data.

    * `default_root` — where the default profile keeps its files (`…/Code/User`); the policy for a
      profile's file is the default profile's `PresetFile` at `default_root/<file>` (decision D4).
    * `home` — the directory that holds `<id>/` per profile (`…/Code/User/profiles`). The logical path
      and the real path share this prefix.
    * `index` — the app's own file that lists the profiles. It is read in memory for that list and is
      **never** collected, sealed or tracked.
    * `files` — what a profile may hold.
    """

    default_root: str
    home: str
    index: str
    files: tuple[ProfileFile, ...]

    @classmethod
    def create(cls, *, default_root: str, home: str, index: str, files: tuple[ProfileFile, ...]) -> ProfileSpec:
        """Validate and freeze the layout; never assign fields afterwards."""
        try:
            root, profiles_home, index_path = validate_path(default_root), validate_path(home), validate_path(index)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid profile layout: {exc}") from exc
        if not files:
            raise ConfigError("a profile layout must name at least one file")
        relatives = [file.path for file in files]
        if len({collision_key(relative) for relative in relatives}) != len(relatives):
            raise ConfigError(f"a profile layout names one file twice: {relatives}")
        return cls(default_root=root, home=profiles_home, index=index_path, files=tuple(files))

    def logical_path(self, name: str, relative: str) -> str:
        """The record path for `relative` inside the profile called `name`."""
        return f"{self.home}/{encode_profile_name(name)}/{validate_path(relative)}"

    def default_path(self, relative: str) -> str:
        """Where the default profile keeps `relative`: the counterpart whose policy a profile reuses."""
        return f"{self.default_root}/{relative}"

    def parse(self, path: str) -> ProfilePath | None:
        """Take a logical path apart, or `None` when `path` is not a well-formed one of this layout.

        Well-formed means: under `home`, a canonical `@<name>` element, and a file this layout lists
        (a glob entry accepts any file its pattern matches). A raw id element is not accepted — it
        has no marker — so a path that arrived with a real id returns `None`.
        """
        # The home is matched exactly, not folded: a logical path is only ever written by this code, so
        # a differently cased spelling is not ours, and accepting it would let a record choose the case
        # `apply` creates on a case-sensitive volume.
        parts, home = path.split("/"), self.home.split("/")
        if len(parts) < len(home) + 2 or parts[: len(home)] != home:
            return None
        name = decode_profile_name(parts[len(home)])
        relative = "/".join(parts[len(home) + 1 :])
        if name is None or relative == "":
            return None
        try:
            validate_path(relative)
        except ManifestCorrupt:
            return None
        file = next((candidate for candidate in self.files if _file_matches(candidate.path, relative)), None)
        if file is None:
            return None
        return ProfilePath(
            name=name, relative=relative, resource=file.resource, default_path=self.default_path(relative)
        )


def _file_matches(pattern: str, relative: str) -> bool:
    """True when `relative` is the file `pattern` names: exactly, or by a last-element glob.

    The directory and an exact name are compared as written; only a glob's last element is folded,
    the way `PresetGlob` folds it. A candidate whose own last element still holds a metacharacter is
    never a match, so a record carrying the pattern itself as a path cannot make `apply` create a file
    called `*.json`.
    """
    directory, _, name = relative.rpartition("/")
    pattern_directory, _, pattern_name = pattern.rpartition("/")
    if directory != pattern_directory or any(character in name for character in "*?"):
        return False
    if not any(character in pattern_name for character in "*?"):
        return name == pattern_name
    expression = "".join(
        "[^/]*" if character == "*" else "[^/]" if character == "?" else re.escape(character)
        for character in collision_key(pattern_name)
    )
    return re.fullmatch(expression, collision_key(name)) is not None


@dataclass(frozen=True)
class ProfileEntry:
    """One profile this machine really has: its name, the directory under `profiles/`, and what it borrows.

    `directory` is a single element in VS Code's id shape (`PROFILE_ID`); anything else — the
    two-element `builtin/agents`, `..`, an absolute path — is refused by the type, so a resolved entry
    can only point inside `home`. `borrowed` holds the resources the profile takes from the default
    profile (`useDefaultFlags`).
    """

    name: str
    directory: str
    borrowed: frozenset[str] = frozenset()

    @classmethod
    def create(cls, *, name: str, directory: str, borrowed: Iterable[str] = ()) -> ProfileEntry:
        """Validate and freeze one entry."""
        encode_profile_name(name)
        if isinstance(borrowed, str) or not all(isinstance(each, str) and each != "" for each in borrowed):
            raise ConfigError(f"borrowed resources must be a list of non-empty names, got {borrowed!r}")
        if not isinstance(directory, str) or PROFILE_ID.fullmatch(directory) is None:
            raise ConfigError(f"a profile directory must be a single hex id, got {directory!r}")
        return cls(name=name, directory=directory, borrowed=frozenset(borrowed))


@dataclass(frozen=True)
class ProfileMap:
    """The profiles this machine has, by name, plus what could not be resolved and why.

    A value built once per cycle from the app's own index. Two names that fold to one are refused
    here, because which directory a record belongs to would otherwise depend on order. `readable`
    is `False` when the index could not be read at all: nothing resolves then, and the caller keeps
    every record rather than treating a missing profile as a deletion (``docs/design/03-manifest.md``).
    """

    entries: tuple[ProfileEntry, ...]
    problems: tuple[str, ...] = ()
    readable: bool = True

    @classmethod
    def create(cls, entries: tuple[ProfileEntry, ...], problems: tuple[str, ...] = ()) -> ProfileMap:
        """Freeze a readable map; refuse names that are one profile under `collision_key`."""
        repeated = duplicate_profile_names(entry.name for entry in entries)
        if repeated:
            raise ConfigError(f"two profiles share one name: {list(repeated)}")
        shared = duplicate_paths(entry.directory for entry in entries)
        if shared:
            # Two names on one directory would write the same files twice, and which write wins would
            # depend on order.
            raise ConfigError(f"two profiles share one directory: {list(shared)}")
        return cls(entries=tuple(entries), problems=tuple(problems), readable=True)

    @classmethod
    def unreadable(cls, reason: str) -> ProfileMap:
        """The map for an index that is missing, unreadable or corrupt: no profile resolves."""
        return cls(entries=(), problems=(reason,), readable=False)

    def lookup(self, name: str) -> ProfileEntry | None:
        """The entry whose name is `name` under `collision_key`, or `None`."""
        key = profile_name_key(name)
        return next((entry for entry in self.entries if profile_name_key(entry.name) == key), None)


__all__ = [
    "PROFILE_ID",
    "PROFILE_MARK",
    "ProfileEntry",
    "ProfileFile",
    "ProfileMap",
    "ProfilePath",
    "ProfileSpec",
    "decode_profile_name",
    "duplicate_profile_names",
    "encode_profile_name",
    "profile_name_key",
]
