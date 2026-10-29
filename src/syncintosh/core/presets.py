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

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.keyed_array import ArrayRule
from syncintosh.core.manifest import FileFormat, validate_format, validate_path
from syncintosh.core.paths import collision_key, duplicate_paths
from syncintosh.core.tree import KeyPath

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


def _has_glob(path: str) -> bool:
    """True when the **last** element of `path` carries a glob metacharacter."""
    return any(character in path.rpartition("/")[2] for character in GLOB_METACHARACTERS)


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
        return directory == collision_key(self.directory) and self.matcher.fullmatch(name) is not None

    def expand(self, names: Iterable[str]) -> tuple[str, ...]:
        """The concrete `directory/name` paths among `names` this glob matches, sorted.

        `names` are the last elements a directory listing reported (`LiveFiles.list_dir`), so the
        result is the paths a collect can treat exactly like an item's own path.
        """
        matched = sorted(name for name in names if self.matcher.fullmatch(collision_key(name)) is not None)
        return tuple(f"{self.directory}/{name}" if self.directory else name for name in matched)


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
    """

    path: str
    format: FileFormat
    noise: frozenset[KeyPath]
    excluded: frozenset[KeyPath]
    validator: ValidatorName | None
    glob: PresetGlob | None
    arrays: tuple[tuple[KeyPath, ArrayRule], ...] = ()

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
        noise: Iterable[KeyPath] = (),
        excluded: Iterable[KeyPath] = (),
        validator: ValidatorName | None = None,
        array_identity: Mapping[KeyPath, ArrayRule] | None = None,
    ) -> PresetFile:
        """Validate and freeze one preset file; never assign fields afterwards."""
        try:
            validated_path = validate_path(path)
            validated_format = validate_format(format)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid preset file: {exc}") from exc
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
        if (noise_paths or excluded_paths) and validated_format not in KEYED_FORMATS:
            raise ConfigError(f"{validated_path}: noise/excluded keys need a keyed format, not {validated_format!r}")
        repeated = noise_paths & excluded_paths
        if repeated:
            raise ConfigError(f"{validated_path}: a key cannot be both noise and excluded: {sorted(repeated)}")
        return cls(
            path=validated_path,
            format=validated_format,
            noise=noise_paths,
            excluded=excluded_paths,
            validator=validator,
            glob=glob,
            arrays=tuple(sorted((array_identity or {}).items(), key=lambda item: item[0])),
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
    def stripped(self) -> frozenset[KeyPath]:
        """Every key path this file removes before sealing, noise and excluded together."""
        return self.noise | self.excluded


@dataclass(frozen=True)
class Preset:
    """A built-in app preset: its files and the metadata for detecting and pointing at the app.

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

    @classmethod
    def create(
        cls,
        *,
        name: str,
        files: tuple[PresetFile, ...],
        bundle_id: str | None = None,
        download_url: str | None = None,
        cask: str | None = None,
    ) -> Preset:
        """Validate and freeze a preset; never assign fields afterwards."""
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
        return cls(name=name, files=files, bundle_id=bundle_id, download_url=download_url, cask=cask)


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
