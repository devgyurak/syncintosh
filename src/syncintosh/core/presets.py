# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preset catalog's shape: which paths an app owns, what to strip, and how to validate.

A preset answers three questions that fail **silently** when they are wrong, so this module
keeps them as validated, frozen data rather than prose:

* **paths** — a wrong path collects nothing, and "nothing to sync" looks like success;
* **noise keys** — a machine- or session-specific key turns into a conflict on every sync;
* **exclusions** — a credential (`auths`, a TLS private key) that reaches a repo must never
  have been collected at all (``AGENTS.md`` §3.4).

The data lives in `syncintosh.presets`; the shape and its rules live here, so `core` stays
pure and a catalog cannot describe something the projection rejects (``AGENTS.md`` §2.1).

`noise` and `excluded` are **key paths** into the file's tree, and they are only meaningful for
a format with a key-path tree — the merge engine's unit of comparison
(:mod:`syncintosh.core.tree`). Setting either on `text` (or `binary`) is a configuration bug,
not a no-op, so it is refused here instead of quietly collecting the whole file.

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from syncintosh.core.agent_projects import AgentProjectSpec
from syncintosh.core.config_dirs import ConfigDirSpec
from syncintosh.core.documents import DocumentSource
from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.manifest import FileFormat, validate_format, validate_path
from syncintosh.core.paths import collision_key, duplicate_paths
from syncintosh.core.profiles import ProfileSpec
from syncintosh.core.tree import AnyKey, KeyPath, KeyPattern, matches_pattern, pattern_sort_key

# The syntax checkers a preset may name. A value is serialized verbatim, so it is a `Literal`
# and never an `Enum` (``AGENTS.md`` §4); `services` looks the implementation up in a mapping
# the `cli` fills in, so `core` never names `system`. `json` is the strict JSONC one: a file whose
# reader is a strict JSON parser (Docker's `config.json`) must not be written when a render left it
# with, say, a trailing comma — refusing is what ``AGENTS.md`` §3.5 asks for.
ValidatorName = Literal["json", "zsh"]

# The formats whose parse produces a key-path tree. `noise`/`excluded` are only expressible for
# these; `text` and `binary` are compared as a whole file and have no keys to strip.
KEYED_FORMATS: tuple[FileFormat, ...] = ("jsonc", "toml", "yaml", "xml", "plist")

# The one place a glob may appear and the one character that is refused. `*` and `?` are allowed in
# the **last** path element only; `**` is refused, because a recursive pattern would let one preset
# claim a whole tree (including directories it does not own) and turn the collect-side expansion from
# one directory listing into an unbounded walk.
GLOB_METACHARACTERS: tuple[str, ...] = ("*", "?")
_RECURSIVE_GLOB: Final = "**"

# How many files a glob publishes. A glob over a directory that happens to hold thousands of files
# is a configuration mistake, and holding back the ones past the bound (and saying so) is better than
# publishing an unbounded set: the bound is the user's to raise by narrowing the pattern. It is a
# **soft** guard — a file already a record keeps being collected, so a sync that pushes the total over
# the bound cannot leave the glob permanently refused (`services/sync.py`).
MAX_GLOB_EXPANSION: Final = 256


# A path element that stands for exactly one UUID (`{uuid}.jsonl`, `{uuid}/subagents/…`). Only an append-only file may
# use it: it is how a preset owns a session's files without owning every folder name under a project.
UUID_TOKEN: Final = "{uuid}"
_UUID_REGEX: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _has_glob(path: str) -> bool:
    """True when the **last** element of `path` carries a glob metacharacter."""
    return any(character in path.rpartition("/")[2] for character in GLOB_METACHARACTERS)


def _has_pattern(path: str) -> bool:
    """True when **any** element of `path` carries a metacharacter or the UUID token (an append-only path)."""
    return UUID_TOKEN in path or any(character in path for character in GLOB_METACHARACTERS)


def _deep_to_regex(path: str) -> str:
    """The regex a path pattern means, element by element: `*` and `?` never cross a `/`, `{uuid}` is one UUID.

    Matched against the path **as written**, never folded: an agent's own folder names are exact, so a record that
    spells a UUID in capitals names a different folder on a case-sensitive volume and must not be taken for the
    lowercase one the agent made.
    """
    folded = path.replace(UUID_TOKEN, "\0")
    return "".join(
        "[^/]*"
        if character == "*"
        else "[^/]"
        if character == "?"
        else _UUID_REGEX
        if character == "\0"
        else re.escape(character)
        for character in folded
    )


def _glob_to_regex(name: str) -> str:
    """The regex a last-element glob means: `*` any run, `?` one character, everything else literal."""
    return "".join(
        "[^/]*" if character == "*" else "[^/]" if character == "?" else re.escape(character) for character in name
    )


@dataclass(frozen=True)
class PresetGlob:
    """A preset path whose last element is a glob (`*`, `?`), never recursive.

    A glob is how a preset owns a **directory** of files that differ per machine — VS Code's
    `snippets/*.json`. The pattern is matched on the folded path (`collision_key`), the same way an
    exact path is compared everywhere else, so `.JSON` and `.json` name one set on a
    case-insensitive APFS. Only regular files and symlinks are ever listed (`LiveFiles.list_dir`);
    a symlink is reported rather than followed. The last element is the only place a metacharacter
    may sit, which is why the expansion is one listing rather than a walk.
    """

    path: str
    directory: str
    name: str
    matcher: re.Pattern[str]
    # Set only for a pattern in a directory element (an append-only path): the whole folded path is matched, and the
    # pattern is never expanded by a directory listing — the session store enumerates, `core/sessions.py`.
    whole: re.Pattern[str] | None = None

    @classmethod
    def deep(cls, path: str) -> PresetGlob:
        """Validate and freeze a pattern that may sit in any element; never recursive, and never expanded by a walk."""
        validated = validate_path(path)
        if _RECURSIVE_GLOB in validated:
            raise ConfigError(f"a preset glob must not be recursive: {validated!r}")
        if not _has_pattern(validated):
            raise ConfigError(f"a preset glob must name a pattern: {validated!r}")
        directory, _, name = validated.rpartition("/")
        return cls(
            path=validated,
            directory=directory,
            name=name,
            matcher=re.compile(_glob_to_regex(collision_key(name))),
            whole=re.compile(_deep_to_regex(validated)),
        )

    @classmethod
    def create(cls, path: str) -> PresetGlob:
        """Validate and freeze one glob; never assign fields afterwards."""
        validated = validate_path(path)
        directory, _, name = validated.rpartition("/")
        if any(character in directory for character in GLOB_METACHARACTERS):
            # A metacharacter outside the last element would name a literal directory (no walk
            # happens, since the directory is listed verbatim), but a preset should never name such
            # a directory, so it is refused rather than silently treated as an exact string.
            raise ConfigError(f"a preset glob may only pattern the last path element: {validated!r}")
        if _RECURSIVE_GLOB in name:
            raise ConfigError(f"a preset glob must not be recursive: {validated!r}")
        if not _has_glob(validated):
            raise ConfigError(f"a preset glob must name a pattern: {validated!r}")
        return cls(
            path=validated,
            directory=directory,
            name=name,
            matcher=re.compile(_glob_to_regex(collision_key(name))),
        )

    def matches(self, path: str) -> bool:
        """True when `path` is a file this glob names: same directory and a matching last element.

        A candidate whose **own** last element still holds a metacharacter is not a file this glob
        names. Without that check the pattern text matches itself — ``*.json`` full-matches
        ``[^/]*\\.json`` — so a record that arrived from the repo carrying the pattern as its path
        could make `apply` create a file literally called ``*.json``.
        """
        directory, _, name = collision_key(path).rpartition("/")
        if any(character in name for character in GLOB_METACHARACTERS):
            return False
        if self.whole is not None:
            return not any(character in directory for character in GLOB_METACHARACTERS) and (
                self.whole.fullmatch(path) is not None
            )
        return directory == collision_key(self.directory) and self.matcher.fullmatch(name) is not None

    def expand(self, names: Iterable[str]) -> tuple[str, ...]:
        """The concrete `directory/name` paths among `names` this glob matches, sorted.

        `names` are the last elements a directory listing reported (`LiveFiles.list_dir`), so the
        result is the paths a collect can treat exactly like an item's own path.
        """
        matched = sorted(name for name in names if self.matcher.fullmatch(collision_key(name)) is not None)
        return tuple(f"{self.directory}/{name}" if self.directory else name for name in matched)


def _check_allowlist(
    path: str, allowed: frozenset[KeyPath], excluded: frozenset[KeyPattern], format: FileFormat, ignore_list: bool
) -> None:
    """Refuse an allowlist that would disable the file, contradict an exclusion, or cannot be applied."""
    if not allowed:
        # An empty allowlist makes every key local: the file would be collected as `{}` forever, which reads as
        # a working preset that syncs nothing.
        raise ConfigError(f"{path}: an allowlist must name at least one key")
    if () in allowed:
        raise ConfigError(f"{path}: an allowlisted key path must name a key below the root")
    if any(not isinstance(segment, str) for key in allowed for segment in key):
        # The allowlist is exact: a wildcard segment would match no real key, so the file would silently sync nothing.
        raise ConfigError(f"{path}: an allowlisted key path takes literal keys only, not a wildcard")
    if format not in KEYED_FORMATS:
        raise ConfigError(f"{path}: an allowlist needs a keyed format, not {format!r}")
    if ignore_list:
        # The ignore list carries exact top-level keys beside the keys the preset strips; an allowlist that cut
        # the rest would leave it to decide, per key, which side of two rules a key falls on.
        raise ConfigError(f"{path}: an allowlist cannot be combined with an ignore list")
    clashing = sorted(key for key in allowed if any(matches_pattern(key, pattern) for pattern in excluded))
    if clashing:
        # An excluded key is in no object at all; naming it (or a key beneath it) as synced says the opposite,
        # and which wins must not depend on the reader.
        raise ConfigError(f"{path}: an allowlisted key is also excluded: {clashing}")


def _check_append_only(
    path: str, format: FileFormat, stripped: frozenset[KeyPattern], validator: ValidatorName | None, ignore_list: bool
) -> None:
    """Refuse an append-only file that asks for something only a keyed or rewritten file can have."""
    if format != "text":
        # The prefix rule compares bytes. A keyed format is merged by key, and `binary` is opaque on purpose.
        raise ConfigError(f"{path}: an append-only file must be `text`, not {format!r}")
    if stripped:
        # Stripping rewrites the file, so the stored bytes would no longer be a prefix of the live ones.
        raise ConfigError(f"{path}: an append-only file cannot strip noise or excluded keys")
    if ignore_list:
        raise ConfigError(f"{path}: an append-only file cannot be an ignore-list file")
    if validator is not None:
        # A transcript is not a script; and a check that fails on a half-written tail would hold the file back.
        raise ConfigError(f"{path}: an append-only file cannot name a syntax check")


@dataclass(frozen=True)
class PresetFile:
    """One file a preset owns, with the keys to strip and the checker to run.

    `noise` keys are machine- or session-specific values: they are removed before the object is
    sealed and re-attached from the live file on apply, so a change to one produces neither a
    diff nor a conflict (``docs/design/02-base-and-merge.md``, "Projection").

    `excluded` keys are the stricter set: a credential that must never enter the repo at all.
    The two share a mechanism but not a meaning — `noise` is convenience, `excluded` is a
    security invariant (``AGENTS.md`` §3.4) — so they stay separate.

    `arrays` names the arrays inside the file that merge element by element instead of as one value (see
    `core/keyed_array.py`). It is part of the local catalog only: the policy is read by path from the
    preset that owns the file, never from a synced copy.

    `ignore_list` marks the file that holds the user's own list of settings to keep to themselves
    (VS Code's ``settingsSync.ignoredSettings``) and obeys it (`core/ignore.py`). The list is read from
    this exact path, and the files a profile takes this policy for obey the same list.

    `synced` is an allowlist, the opposite way round: when it is set, **only** the keys at or beneath its paths
    travel, and every other key is local — cut before sealing and put back from the live file on apply, as a
    noise key is. It is for an app whose file mixes a few shared preferences with state and credentials, and
    that adds keys over time: a deny-list would carry each new key until someone noticed, the allowlist holds it
    back until the preset names it. `noise` and `excluded` still apply inside it. `None` is no allowlist.

    `append_only` marks a file that is only ever extended — a session transcript (`core/append.py`). It is merged by
    the prefix rule instead of a line-level three-way merge, is applied only as a missing file or a strict extension of
    the local one, and is never restore-pointed. Like `arrays` it shapes how a merge reads the file, not which bytes an
    object holds, so it is not in the policy digest. It needs a whole-file format with nothing to strip.
    """

    path: str
    format: FileFormat
    noise: frozenset[KeyPattern]
    excluded: frozenset[KeyPattern]
    validator: ValidatorName | None
    glob: PresetGlob | None
    arrays: tuple[tuple[KeyPath, ArrayRule], ...] = ()
    ignore_list: bool = False
    synced: frozenset[KeyPath] | None = None
    append_only: bool = False

    @property
    def array_rules(self) -> dict[KeyPath, ArrayRule]:
        """The element-level rules by array path, as a fresh mapping the caller may not mutate in place."""
        return dict(self.arrays)

    @classmethod
    def create(
        cls,
        *,
        path: str,
        format: FileFormat,
        noise: Iterable[KeyPattern] = (),
        excluded: Iterable[KeyPattern] = (),
        validator: ValidatorName | None = None,
        array_identity: Mapping[KeyPath, ArrayRule] | None = None,
        ignore_list: bool = False,
        synced: Iterable[KeyPath] | None = None,
        append_only: bool = False,
    ) -> PresetFile:
        """Validate and freeze one preset file; never assign fields afterwards."""
        try:
            validated_path = validate_path(path)
            validated_format = validate_format(format)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid preset file: {exc}") from exc
        if append_only and _has_pattern(validated_path):
            # A session's files sit under a folder named by the agent (a UUID), so the pattern may be in any element.
            glob = PresetGlob.deep(validated_path)
        else:
            glob = PresetGlob.create(validated_path) if _has_glob(validated_path) else None
        if glob is None and any(character in validated_path for character in GLOB_METACHARACTERS):
            # A metacharacter outside the last element is neither an exact path nor a glob, so it is
            # refused rather than read as a literal directory name.
            raise ConfigError(f"a glob metacharacter may only appear in the last path element: {validated_path!r}")
        noise_paths = frozenset(noise)
        excluded_paths = frozenset(excluded)
        if () in noise_paths or () in excluded_paths:
            # The empty path is the document root, so "strip" would remove the whole file and
            # the projection would be a constant. Refuse it instead of disabling the file silently.
            raise ConfigError(f"{validated_path}: a key path must name a key below the root")
        if any(all(isinstance(segment, AnyKey) for segment in pattern) for pattern in noise_paths | excluded_paths):
            # Every segment a wildcard names every key at that depth, which is the document's whole content
            # one level down: stripping it would disable the file as surely as the empty path does.
            raise ConfigError(f"{validated_path}: a key pattern must name at least one literal key")
        if (noise_paths or excluded_paths) and validated_format not in KEYED_FORMATS:
            raise ConfigError(f"{validated_path}: noise/excluded keys need a keyed format, not {validated_format!r}")
        if ignore_list and (glob is not None or validated_format not in KEYED_FORMATS):
            # The list is read from one file, as keys of a document: a pattern names no single file, and a
            # format without keys has no list to read.
            raise ConfigError(f"{validated_path}: an ignore list needs one exact path in a keyed format")
        if ignore_list and any(
            any(isinstance(segment, AnyKey) for segment in pattern) for pattern in noise_paths | excluded_paths
        ):
            # The ignore list carries exact top-level keys (`core/ignore.py`); a wildcard beside it would blur
            # which keys are carried and which are stripped.
            raise ConfigError(f"{validated_path}: an ignore-list file cannot use a wildcard key pattern")
        allowed = None if synced is None else frozenset(synced)
        if allowed is not None:
            _check_allowlist(validated_path, allowed, excluded_paths, validated_format, ignore_list)
        if append_only:
            _check_append_only(validated_path, validated_format, noise_paths | excluded_paths, validator, ignore_list)
            if allowed is not None or array_identity:
                # An allowlist and element rules read a file as keys; an append-only file has none.
                raise ConfigError(f"{validated_path}: an append-only file cannot use an allowlist or array rules")
        repeated = noise_paths & excluded_paths
        if repeated:
            raise ConfigError(
                f"{validated_path}: a key cannot be both noise and excluded: {sorted(repeated, key=pattern_sort_key)}"
            )
        return cls(
            path=validated_path,
            format=validated_format,
            noise=noise_paths,
            excluded=excluded_paths,
            validator=validator,
            glob=glob,
            arrays=tuple(sorted((array_identity or {}).items(), key=lambda item: item[0])),
            ignore_list=ignore_list,
            synced=allowed,
            append_only=append_only,
        )

    @property
    def is_glob(self) -> bool:
        """True when this file names a directory pattern rather than one exact path."""
        return self.glob is not None

    def matches(self, path: str) -> bool:
        """True when `path` is this file: the glob it names, or the exact path it names."""
        if self.glob is not None:
            return self.glob.matches(path)
        return collision_key(self.path) == collision_key(path)

    @property
    def stripped(self) -> frozenset[KeyPattern]:
        """Every key pattern this file removes before sealing, noise and excluded together."""
        return self.noise | self.excluded

    @property
    def strips(self) -> bool:
        """True when projecting this file can remove anything: a stripped key pattern or an allowlist."""
        return bool(self.stripped) or self.synced is not None


@dataclass(frozen=True)
class Preset:
    """A built-in app preset: its files and the metadata for detecting and pointing at the app.

    `policy_epoch` is bumped by hand when the **code** that projects or carries this preset's files changes
    meaning (`core/projection.py`, `core/ignore.py`, a re-enabled strip): the object bytes then differ although
    no data field does, so the digest (`core/policy.py`) would not see it. It is part of the digest; bump it,
    then append the new digest to `POLICY_HISTORY`.

    `config_dir` says the files' paths are **logical** under a versioned folder (`core/config_dirs.py`), and
    `defer_while_running` holds back only the apply of this preset's files while the app is open (an IDE rewrites
    its settings on quit, so a file written under it would be overwritten). Both are local behaviour, not part of
    the object bytes, so neither is in the policy digest.

    `documents` are database rows this preset's files are read from and written to (`core/documents.py`): each
    source's virtual view path is one of the preset's exact files. Like `config_dir` it is local behaviour, not part
    of the object bytes, so it is not in the policy digest.

    `agent_projects` says the files' paths are **logical** under a per-project folder (`core/agent_projects.py`): each
    file is listed with the bare marker for the project's element (`.claude/projects/@/memory/*.md`) and is written
    once per project this machine's config enabled it for. Local behaviour like `config_dir`, so not in the digest.

    `download_url` is the vendor's official page. Only a URL hard-coded here is trusted
    (``AGENTS.md`` §3.6), so the field is restricted to `https`. A deferral report reads it
    directly (`services.sync`); a URL that arrived from anywhere else is screened through
    :func:`download_trust`. Both call sites rely on the caller passing the **shipped** catalog.
    """

    name: str
    files: tuple[PresetFile, ...]
    bundle_id: str | None
    download_url: str | None
    cask: str | None
    profiles: ProfileSpec | None = None
    policy_epoch: int = 1
    config_dir: ConfigDirSpec | None = None
    defer_while_running: bool = False
    documents: tuple[DocumentSource, ...] = ()
    agent_projects: AgentProjectSpec | None = None

    @classmethod
    def create(
        cls,
        *,
        name: str,
        files: tuple[PresetFile, ...],
        bundle_id: str | None = None,
        download_url: str | None = None,
        cask: str | None = None,
        profiles: ProfileSpec | None = None,
        policy_epoch: int = 1,
        config_dir: ConfigDirSpec | None = None,
        defer_while_running: bool = False,
        documents: tuple[DocumentSource, ...] = (),
        agent_projects: AgentProjectSpec | None = None,
    ) -> Preset:
        """Validate and freeze a preset; never assign fields afterwards."""
        if not isinstance(policy_epoch, int) or isinstance(policy_epoch, bool) or policy_epoch < 1:
            raise ConfigError(f"preset {name!r}: policy_epoch must be a positive integer, got {policy_epoch!r}")
        if not isinstance(name, str) or name == "":
            raise ConfigError(f"preset name must be a non-empty string, got {name!r}")
        if not files:
            raise ConfigError(f"preset {name!r} owns no files")
        duplicated = duplicate_paths(file.path for file in files)
        if duplicated:
            # Two entries for one file (case- or normalization-differing) would double the
            # record and write it twice.
            raise ConfigError(f"preset {name!r} names one path twice: {list(duplicated)}")
        if download_url is not None and urlsplit(download_url).scheme != "https":
            raise ConfigError(f"preset {name!r}: download URL must be https, got {download_url!r}")
        if profiles is not None:
            # A profile reuses the default profile's policy for each of its files (decision D4), so a file
            # a profile may hold must be one this preset owns: otherwise the lookup would find no policy
            # and the file would be collected unstripped.
            owned = {file.path for file in files}
            stray = [file.path for file in profiles.files if profiles.default_path(file.path) not in owned]
            if stray:
                raise ConfigError(f"preset {name!r}: profile files without a default counterpart: {stray}")
            if any(file.matches(profiles.index) for file in files):
                # The index is read in memory for the profile list only; it also holds window state and a
                # telemetry machine id, so a preset must never be able to collect and seal it.
                raise ConfigError(f"preset {name!r}: the profile index must not be a tracked file")
        if config_dir is not None:
            if profiles is not None:
                # Both would claim a logical `@` element under one app, and which resolver a path takes would
                # depend on order.
                raise ConfigError(f"preset {name!r}: a config directory and profiles cannot be combined")
            if any(file.ignore_list for file in files):
                # The ignore list is read from the file's own path, which here is logical text and not a real
                # file, so it would silently be empty and carry nothing.
                raise ConfigError(f"preset {name!r}: a config directory cannot hold an ignore-list file")
            if bundle_id != config_dir.bundle_id:
                # The release folder is read from the app this preset detects; two spellings would let the
                # detection and the folder name come from different apps.
                raise ConfigError(f"preset {name!r}: the config directory's bundle id must be the preset's")
            stray = [file.path for file in files if not config_dir.contains(file.path)]
            if stray:
                # A path outside the logical root would be written verbatim, never through the resolved folder.
                raise ConfigError(f"preset {name!r}: files outside the config directory: {stray}")
        if documents:
            if profiles is not None or config_dir is not None:
                # Each of the three owns a logical `@` element, and which resolver a path took would depend on order.
                raise ConfigError(f"preset {name!r}: documents cannot be combined with profiles or a config directory")
            exact = {file.path for file in files if not file.is_glob}
            stray = [source.view for source in documents if source.view not in exact]
            if stray:
                # A view the preset does not own as an exact file would be routed to the database with no policy.
                raise ConfigError(f"preset {name!r}: a document's view is not one of its files: {stray}")
            if duplicate_paths(source.view for source in documents):
                raise ConfigError(f"preset {name!r}: two documents share one view path")
            if any(file.ignore_list and file.path in {source.view for source in documents} for file in files):
                # The ignore list is read from the file's own path, which here is virtual.
                raise ConfigError(f"preset {name!r}: a document cannot be an ignore-list file")
        if agent_projects is not None:
            if profiles is not None or config_dir is not None or documents:
                # Each of the four owns a logical `@` element, and which resolver a path took would depend on order.
                raise ConfigError(
                    f"preset {name!r}: agent projects cannot be combined with profiles, a config directory or documents"
                )
            stray = [file.path for file in files if agent_projects.relative_of_template(file.path) is None]
            if stray:
                # A file outside the project's element would be written verbatim, never through the resolved folder.
                raise ConfigError(f"preset {name!r}: files outside the agent project layout: {stray}")
            if any(file.ignore_list for file in files):
                # The ignore list is read from the file's own path, which here is a template and not a real file.
                raise ConfigError(f"preset {name!r}: an agent project preset cannot hold an ignore-list file")
        if any(file.append_only for file in files) and (profiles is not None or config_dir is not None or documents):
            # An append-only file is applied only when it is missing or extends the local one, and the profile, config
            # directory and document resolvers each decide on a directory or a database row this rule does not know.
            raise ConfigError(
                f"preset {name!r}: append-only files cannot be combined with profiles, a config directory or documents"
            )
        if defer_while_running and bundle_id is None:
            raise ConfigError(f"preset {name!r}: deferring while running needs a bundle id to ask about")
        return cls(
            name=name,
            files=files,
            bundle_id=bundle_id,
            download_url=download_url,
            cask=cask,
            profiles=profiles,
            policy_epoch=policy_epoch,
            config_dir=config_dir,
            defer_while_running=defer_while_running,
            documents=documents,
            agent_projects=agent_projects,
        )


@dataclass(frozen=True)
class ShippedPresets:
    """The presets this build ships: the only set a download URL can be trusted against.

    Built through :meth:`create`. The presets package hands one out (`BuiltinCatalog.shipped`), and
    nothing else should: a synced or community file that arrives as presets must not be wrapped in
    this type, because wrapping it is what would make its URLs trusted (``AGENTS.md`` §3.6).
    """

    presets: tuple[Preset, ...]

    @classmethod
    def create(cls, presets: tuple[Preset, ...]) -> ShippedPresets:
        """Freeze the shipped presets. The caller is the trust boundary, not this constructor."""
        return cls(presets=presets)


@dataclass(frozen=True)
class DownloadTrust:
    """Whether a download URL is one of the built-in presets', and the domain to show.

    The domain is always returned — even when untrusted — because a URL that arrived from an
    LLM or a synced preset must be **labelled** and have its domain shown prominently, and the
    caller cannot extract it without parsing the URL itself (``AGENTS.md`` §3.6).
    """

    trusted: bool
    domain: str


def download_trust(url: str, *, shipped: ShippedPresets) -> DownloadTrust:
    """Decide whether `url` is a shipped preset's download page.

    Trust is **exact string equality** with a hard-coded URL, never a domain or suffix match:
    `https://evil.example/?code.<vendor>` and `https://<vendor>.evil.example/` both share a
    substring with a real page, and only the exact URL is one this project shipped.

    `shipped` is a :class:`ShippedPresets`, which only the shipped catalog produces. A `Preset`
    carries no marker of its own, so the type is what keeps a community URL from being trusted
    (``AGENTS.md`` §3.6).
    """
    domain = urlsplit(url).hostname or ""
    return DownloadTrust(trusted=any(preset.download_url == url for preset in shipped.presets), domain=domain)


# ASCII only: a non-ASCII host (a full-width dot or slash) makes `urlsplit` raise on the netloc, and
# a proposal from a model must never be able to crash the resolve that reads it.
# Case-insensitive on the scheme only: with IGNORECASE on the whole pattern, the negated range below
# would also exclude `i`, `l` and `s` through their Unicode case variants, and cut a URL short.
_URL: Final = re.compile(r"(?i:https?)://[^\s\"'<>()\[\]{}\x00-\x1f\x7f-\U0010ffff]+")
_UNICODE_ESCAPE: Final = re.compile(r"\\u([0-9a-fA-F]{4})")


@dataclass(frozen=True)
class ProposedUrl:
    """A URL a proposal introduces that no side already had.

    It is never trusted. Trust comes from a shipped preset's exact URL, and a proposal is not a
    shipped preset: a proposed URL that matches a shipped one character for character is still
    untrusted, because the proposal is where it came from (``AGENTS.md`` §3.6). `domain` is shown
    prominently so the person sees where it points.
    """

    url: str
    domain: str
    trusted: Literal[False] = False


def proposed_urls(proposal: str, *, base: str, local: str, remote: str) -> tuple[ProposedUrl, ...]:
    """The URLs in `proposal` that appear in none of the three sides, each as an untrusted URL.

    A URL the sides already held is not new, so it is not flagged here. Every URL this returns
    is new, and every one is untrusted, whatever it matches. A URL is found in the text as written
    and in its JSON-unescaped forms, so `https:\\/\\/` and `\\u0068ttps://` are seen too.
    """
    known = {url for side in (base, local, remote) for url in _urls_in(side)}
    found = dict.fromkeys(url for url in _urls_in(proposal) if url not in known)
    return tuple(ProposedUrl(url=url, domain=_host(url)) for url in found)


def _urls_in(text: str) -> tuple[str, ...]:
    """Every URL in any of the text's forms: as written, with `\\/` unescaped, and with `\\uXXXX` decoded."""
    unslashed = text.replace("\\/", "/")
    decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    unslashed_decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), unslashed)
    forms = (text, unslashed, decoded, unslashed_decoded)
    # Ordered by first appearance, so the same proposal always reports its URLs in the same order.
    return tuple(dict.fromkeys(_clean(url) for form in forms for url in _URL.findall(form)))


def _clean(raw: str) -> str:
    """A matched URL without the sentence punctuation the pattern can swallow after it."""
    return raw.rstrip(".,;:!?")


def _host(url: str) -> str:
    """The host a URL names. The pattern admits no brackets, so `urlsplit` cannot reject one here."""
    return urlsplit(url).hostname or ""


@dataclass(frozen=True)
class EmptyCatalog:
    """A catalog with no presets: nothing is owned, nothing is stripped, nothing is claimed.

    The absence of a policy, not a preset — the value a caller with no built-in catalog passes
    so the projection is the identity and `knows` makes no claim. It holds no data and does no
    I/O, so it sits beside the port rather than in an infrastructure package (``AGENTS.md`` §2.1).
    """

    def policy_for(self, path: str) -> PresetFile | None:
        return None

    def knows(self, preset: str, path: str) -> bool:
        return True

    def get(self, name: str) -> Preset | None:
        return None

    def preset_for(self, path: str) -> Preset | None:
        return None

    def ignore_list_files(self) -> tuple[PresetFile, ...]:
        return ()

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        return None


# The single instance; a frozen dataclass with no fields, so identity and equality agree.
EMPTY_CATALOG: Final = EmptyCatalog()


__all__ = [
    "EMPTY_CATALOG",
    "GLOB_METACHARACTERS",
    "KEYED_FORMATS",
    "MAX_GLOB_EXPANSION",
    "DownloadTrust",
    "EmptyCatalog",
    "Preset",
    "PresetFile",
    "PresetGlob",
    "ProposedUrl",
    "ShippedPresets",
    "ValidatorName",
    "download_trust",
    "proposed_urls",
]
