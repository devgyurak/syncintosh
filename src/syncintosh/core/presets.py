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

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import FileFormat, validate_format, validate_path
from syncintosh.core.paths import duplicate_paths
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


@dataclass(frozen=True)
class PresetFile:
    """One file a preset owns, with the keys to strip and the checker to run.

    `noise` keys are machine- or session-specific values: they are removed before the object is
    sealed and re-attached from the live file on apply, so a change to one produces neither a
    diff nor a conflict (``docs/design/02-base-and-merge.md``, "Projection").

    `excluded` keys are the stricter set: a credential that must never enter the repo at all.
    The two share a mechanism but not a meaning — `noise` is convenience, `excluded` is a
    security invariant (``AGENTS.md`` §3.4) — so they stay separate.
    """

    path: str
    format: FileFormat
    noise: frozenset[KeyPath]
    excluded: frozenset[KeyPath]
    validator: ValidatorName | None

    @classmethod
    def create(
        cls,
        *,
        path: str,
        format: FileFormat,
        noise: Iterable[KeyPath] = (),
        excluded: Iterable[KeyPath] = (),
        validator: ValidatorName | None = None,
    ) -> PresetFile:
        """Validate and freeze one preset file; never assign fields afterwards."""
        try:
            validated_path = validate_path(path)
            validated_format = validate_format(format)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid preset file: {exc}") from exc
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
        )

    @property
    def stripped(self) -> frozenset[KeyPath]:
        """Every key path this file removes before sealing, noise and excluded together."""
        return self.noise | self.excluded


@dataclass(frozen=True)
class Preset:
    """A built-in app preset: its files and the metadata for detecting and pointing at the app.

    `download_url` is the vendor's official page. Only a URL hard-coded here is trusted
    (``AGENTS.md`` §3.6), so the field is restricted to `https` and consumed only through
    :func:`download_trust`.
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
class DownloadTrust:
    """Whether a download URL is one of the built-in presets', and the domain to show.

    The domain is always returned — even when untrusted — because a URL that arrived from an
    LLM or a synced preset must be **labelled** and have its domain shown prominently, and the
    caller cannot extract it without parsing the URL itself (``AGENTS.md`` §3.6).
    """

    trusted: bool
    domain: str


def download_trust(url: str, *, builtin: Iterable[Preset]) -> DownloadTrust:
    """Decide whether `url` is a built-in preset's download page.

    Trust is **exact string equality** with a hard-coded URL, never a domain or suffix match:
    `https://evil.example/?code.<vendor>` and `https://<vendor>.evil.example/` both share a
    substring with a real page, and only the exact URL is one this project shipped.

    `builtin` must be the **shipped** catalog's presets (`BuiltinCatalog.default().presets`).
    A `Preset` carries no marker distinguishing a built-in from one that arrived from a synced
    or community file, so the trust boundary is the call site's: passing a merged list would mark
    a community URL trusted (``AGENTS.md`` §3.6). Tighten this to a catalog-only parameter when
    the first caller lands (#8/#9).
    """
    domain = urlsplit(url).hostname or ""
    return DownloadTrust(trusted=any(preset.download_url == url for preset in builtin), domain=domain)


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


# The single instance; a frozen dataclass with no fields, so identity and equality agree.
EMPTY_CATALOG: Final = EmptyCatalog()


__all__ = [
    "EMPTY_CATALOG",
    "KEYED_FORMATS",
    "DownloadTrust",
    "EmptyCatalog",
    "Preset",
    "PresetFile",
    "ValidatorName",
    "download_trust",
]
