# SPDX-License-Identifier: AGPL-3.0-or-later
"""The policy stamp: which preset policy a repo's objects were made under (issue #24).

Two builds can disagree about what a preset strips, and nothing in the repo used to say which build
published an object. The manifest now carries, per built-in preset, a **stamp** — `(rev, digest)` —
and this module is the whole comparison (``docs/design/03-manifest.md``, "Policy stamp"):

* `digest` is the sha256 of a canonical description of what the preset strips (`policy_digest`);
* `rev` is the position of that digest in the preset's append-only history, which the presets package
  ships (`PresetCatalog.policy_history`), so a build can tell a policy that is **older** than its own
  from one it has never heard of.

`rev` and `digest` are both stated because neither is enough alone: a bare revision cannot tell a
fork's policy 3 from ours, and a bare digest cannot say which way to migrate.

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from syncintosh.core.canonical import canonical_json_bytes
from syncintosh.core.errors import ManifestCorrupt

if TYPE_CHECKING:
    from syncintosh.core.manifest import ManifestRecord
    from syncintosh.core.ports import PresetCatalog
    from syncintosh.core.presets import Preset, PresetFile
    from syncintosh.core.profiles import ProfileSpec

# How this build's policy for a preset relates to the one a stamp names. Serialized nowhere, but
# compared by value across modules, so a `Literal` over `Final` strings rather than an `Enum`
# (``AGENTS.md`` §4).
PolicyVerdict = Literal["match", "older", "newer", "unknown"]
MATCH: Final = "match"
OLDER: Final = "older"
NEWER: Final = "newer"
UNKNOWN: Final = "unknown"

# A stamp is hex of a sha256, the same shape as a record's digest.
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# The keys a serialized stamp holds, and nothing else (`manifest_codec` rejects the rest).
STAMP_KEYS: Final = frozenset({"rev", "digest"})


@dataclass(frozen=True)
class PolicyStamp:
    """One preset's policy as the build that wrote the repo knew it."""

    rev: int
    digest: str

    @classmethod
    def create(cls, *, rev: int, digest: str) -> PolicyStamp:
        """Validate and freeze a stamp; never assign fields afterwards."""
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            raise ManifestCorrupt(f"a policy rev must be a positive integer, got {rev!r}")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise ManifestCorrupt(f"a policy digest must be 64 lowercase hex characters, got {digest!r}")
        return cls(rev=rev, digest=digest)


def _key_paths(paths: Iterable[tuple[str, ...]]) -> list[list[str]]:
    """Key paths as sorted lists, so a frozenset's iteration order cannot reach the digest."""
    return sorted(list(path) for path in paths)


def _file_description(file: PresetFile) -> dict[str, Any]:
    # `arrays`, `validator`: not here. They shape how a merge reads a file and what is checked before
    # a write, not which keys are stripped, so a build that differs in them still agrees on the bytes
    # an object holds.
    return {
        "path": file.path,
        "glob": file.is_glob,
        "format": file.format,
        "noise": _key_paths(file.noise),
        "excluded": _key_paths(file.excluded),
        "ignore_list": file.ignore_list,
    }


def _profiles_description(profiles: ProfileSpec | None) -> dict[str, Any] | None:
    if profiles is None:
        return None
    return {
        "default_root": profiles.default_root,
        "home": profiles.home,
        "index": profiles.index,
        "files": sorted(
            ({"path": file.path, "resource": file.resource} for file in profiles.files),
            key=lambda entry: entry["path"],
        ),
    }


def policy_digest(preset: Preset) -> str:
    """The sha256 of everything about `preset` that decides which bytes an object holds.

    The description is canonical JSON, so the order files were listed in and the order keys were
    written in cannot change the digest. The ignore list (`core/ignore.py`) is deliberately absent:
    the user chooses it and it changes between cycles, so it is a carry, not a build property; only
    the `ignore_list` flag of the file that holds it is part of the policy.
    """
    description = {
        "files": sorted((_file_description(file) for file in preset.files), key=lambda entry: entry["path"]),
        "profiles": _profiles_description(preset.profiles),
    }
    return hashlib.sha256(canonical_json_bytes(description)).hexdigest()


def classify_policy(history: tuple[str, ...] | None, stamp: PolicyStamp | None) -> PolicyVerdict:
    """How this build's policy for one preset compares with the one `stamp` names.

    `history` is the local build's digests for the preset, oldest first, or `None` when the build has
    no such preset. A missing `stamp` is revision 1: every repo written before stamps existed was made
    under the first policy, which is what lets a schema-1 repo be read without a migration step.
    """
    if history is None or len(history) == 0:
        return NEWER
    effective = stamp if stamp is not None else PolicyStamp.create(rev=1, digest=history[0])
    if effective.rev > len(history):
        return NEWER
    if history[effective.rev - 1] != effective.digest:
        return UNKNOWN
    return MATCH if effective.rev == len(history) else OLDER


def policy_verdicts(
    policy: Mapping[str, PolicyStamp], records: Iterable[ManifestRecord], catalog: PresetCatalog
) -> dict[str, PolicyVerdict]:
    """The verdict for every preset a manifest makes a claim about, by preset name.

    A preset is covered when the manifest stamps it, or when one of its records names a preset this
    build ships (an unstamped one is revision 1). A record naming a preset that is neither stamped nor
    built in makes no claim — it is a community preset, and the local allowlist alone decides.
    """
    names = set(policy) | {record.preset for record in records if catalog.policy_history(record.preset) is not None}
    return {name: classify_policy(catalog.policy_history(name), policy.get(name)) for name in sorted(names)}


def stamp_policies(records: Iterable[ManifestRecord], catalog: PresetCatalog) -> dict[str, PolicyStamp]:
    """This build's stamp for every built-in preset that has a record: what a writer records.

    A writer always stamps its own build's policy, never one it read. That is honest only because a
    cycle first brings every object it re-uses to the local policy (migrate an `older` stamp, stop on a
    `newer`/`unknown` one); a caller that stamps without that gate would launder a policy this build
    does not know into a `match`.
    """
    stamps: dict[str, PolicyStamp] = {}
    for name in sorted({record.preset for record in records}):
        preset, history = catalog.get(name), catalog.policy_history(name)
        if preset is not None and history:
            # The digest is computed, not read from the history, so a policy changed without an append
            # stamps a digest the history lacks and reads back as `unknown` rather than as `match`.
            stamps[name] = PolicyStamp.create(rev=len(history), digest=policy_digest(preset))
    return stamps


__all__ = [
    "MATCH",
    "NEWER",
    "OLDER",
    "STAMP_KEYS",
    "UNKNOWN",
    "PolicyStamp",
    "PolicyVerdict",
    "classify_policy",
    "policy_digest",
    "policy_verdicts",
    "stamp_policies",
]
