# SPDX-License-Identifier: AGPL-3.0-or-later
"""A settings **document** that lives in a database row or a preferences domain, seen as one virtual file.

Some apps keep their settings as one JSON value in a database row instead of a file (Orca's
`profile-state.db`), others in a macOS preferences domain that `cfprefsd` owns (iTerm2's
`com.googlecode.iterm2`). `services` only moves bytes between a live path and an object, so such a document is
presented to it as a file at a **logical path** that no real folder holds:

```
Library/Application Support/orca/profiles/local-default/@profile-state/settings.json
```

* The ``@`` element marks the path as virtual, the same device as a profile or a versioned config
  directory (`core/profiles.py`, `core/config_dirs.py`): a real folder is never called that, so a
  virtual path cannot collide with a real one. The manifest schema is unchanged.
* `DocumentSource` is **data** a preset carries: which database, which domain, which virtual path,
  and which `kind` of store understands it. Infrastructure picks the implementation by `kind`;
  `core` and `services` never name a database class (``AGENTS.md`` §2.2).
* The bytes `services` sees are an **indented** rendering of the row's JSON, so a diff, a resolve and
  an LLM prompt do not meet one nine-kilobyte line (`render_view`). A write goes back as the app's
  own compact form (`compact_payload`), with key order kept.
* Writing a value equal to the current one is a no-op (`same_value`): the app counts revisions, and a
  needless bump is a change someone else has to read.
* What the three functions above do is one **codec** per `kind` (`codec_for`): a preferences domain is a plist, not
  JSON, so it is rendered as the canonical XML plist (`core/plist_xml.py`) and compared by that form.

Standard library only (``AGENTS.md`` §2.1).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from syncintosh.core.config_dirs import CONFIG_DIR_MARK
from syncintosh.core.errors import ConfigError, DocumentRefused, ManifestCorrupt
from syncintosh.core.manifest import validate_path
from syncintosh.core.plist_xml import canonical_plist, parse_plist

# The one kind a store ships with. A `Final` string and not an `Enum`: it is serialized verbatim
# (``AGENTS.md`` §4).
ORCA_PROFILE_STATE: Final = "orca-profile-state"
# A macOS preferences domain, read and written through `defaults` (`system/defaults_domains.py`).
CFPREFS_DOMAIN: Final = "cfprefs-domain"

# JS prints a whole-number double without a fraction and switches to an exponent at 1e21.
_JS_INTEGER_LIMIT: Final = 1e21

# An unpaired surrogate. `JSON.stringify` writes one as a `\\udXXX` escape, which Python reads into a lone surrogate
# code point that UTF-8 cannot encode; it is written back the way JS wrote it.
_LONE_SURROGATE: Final = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class DocumentSource:
    """One database row presented as a virtual live file.

    `database` and `view` are `$HOME`-relative. `view` carries an ``@`` element, so it names no real
    path, and it is the path a manifest record holds. `domain` is the row's key in the database.

    `skip_when` names a top-level boolean key of the document: when it is on, the app manages these settings by
    itself (iTerm2's `LoadPrefsFromCustomFolder` makes it load and save its own copy), so this machine leaves the
    document alone, in collect and apply alike (`skip_reason`). `skip_because` is the sentence shown then. Both are
    local behaviour and not part of the object bytes, so neither is in the policy digest.
    """

    kind: str
    database: str
    domain: str
    view: str
    skip_when: str = ""
    skip_because: str = ""

    @classmethod
    def create(
        cls, *, kind: str, database: str, domain: str, view: str, skip_when: str = "", skip_because: str = ""
    ) -> DocumentSource:
        """Validate and freeze one source; never assign fields afterwards."""
        if not isinstance(kind, str) or kind == "":
            raise ConfigError(f"a document source needs a kind, got {kind!r}")
        if not isinstance(domain, str) or domain == "":
            raise ConfigError(f"a document source needs a domain, got {domain!r}")
        try:
            validated_database = validate_path(database)
            validated_view = validate_path(view)
        except ManifestCorrupt as exc:
            raise ConfigError(f"invalid document source: {exc}") from exc
        if not any(part.startswith(CONFIG_DIR_MARK) for part in validated_view.split("/")[:-1]):
            # A view without the marker could be a real file, and a write to it would land beside the database
            # instead of in it.
            raise ConfigError(f"a document's view path needs a logical {CONFIG_DIR_MARK!r} folder: {validated_view!r}")
        if any(character in validated_view for character in ("*", "?")):
            raise ConfigError(f"a document's view path cannot be a glob: {validated_view!r}")
        if validated_view == validated_database:
            raise ConfigError("a document's view path cannot be its database")
        if kind == CFPREFS_DOMAIN and validated_database != f"Library/Preferences/{domain}.plist":
            # The store asks the plist file whether the domain is here and `defaults` for its content; two names for
            # one domain would test one file and write another.
            raise ConfigError(f"a preferences domain's database must be Library/Preferences/{domain}.plist")
        if (skip_when == "") != (skip_because == ""):
            raise ConfigError("a document that can be skipped needs both the key and the sentence that says why")
        if skip_when != "" and kind != CFPREFS_DOMAIN:
            # The rule reads a plist's top-level key; a JSON row has no such decision to read.
            raise ConfigError(f"only a preferences domain can be skipped by a key, not kind {kind!r}")
        return cls(
            kind=kind,
            database=validated_database,
            domain=domain,
            view=validated_view,
            skip_when=skip_when,
            skip_because=skip_because,
        )

    @property
    def view_directory(self) -> str:
        """The `$HOME`-relative folder the view sits in: virtual too, and present exactly when the database is."""
        return self.view.rpartition("/")[0]


@dataclass(frozen=True)
class DocumentRead:
    """A document as read: its payload text and the revision it was read at.

    `revision` is what a later write is checked against, so a write never lands on a document that moved. It is an
    **opaque token**: a database's own counter for a row, a digest of the export for a preferences domain. Only the
    store that issued it compares it, and only for equality.
    """

    payload: str
    revision: str


def _utf8(text: str) -> bytes:
    """`text` as UTF-8, or `DocumentRefused` for what UTF-8 cannot hold (never a raw `UnicodeEncodeError`)."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise DocumentRefused("the document holds text UTF-8 cannot encode") from None


def _dump(value: Any, **options: Any) -> str:
    """`value` as JSON text with non-ASCII kept, and only a lone surrogate re-escaped as the app's JS writes it."""
    text = json.dumps(value, ensure_ascii=False, **options)
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def content_hash(payload: str) -> str:
    """The row's integrity hash: SHA-256 of the payload's UTF-8 bytes, in hex (the app verifies it on every read)."""
    return hashlib.sha256(_utf8(payload)).hexdigest()


def _js_number(text: str) -> float | int:
    """A JSON fraction read the way the app's own JS prints it: a whole value is an integer.

    `JSON.stringify(1)` is `1`, never `1.0`, so a payload the app wrote holds no whole-valued fraction.
    Reading one as a Python float would write it back as `1.0` — a different byte string for the same
    value, which the app would then rewrite.
    """
    value = float(text)
    return int(value) if value.is_integer() and abs(value) < _JS_INTEGER_LIMIT else value


def _refuse_constant(name: str) -> Any:
    raise DocumentRefused("the document holds a value JSON does not allow")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        # Last-wins would drop a value without a word; which duplicate the app meant is not knowable.
        raise DocumentRefused("the document repeats a key")
    return dict(pairs)


def _parse(text: str) -> dict[str, Any]:
    """The JSON object `text` holds, or `DocumentRefused` (fixed messages: the text is the user's settings)."""
    try:
        value = json.loads(
            text, parse_float=_js_number, parse_constant=_refuse_constant, object_pairs_hook=_object_pairs
        )
    except (ValueError, RecursionError):
        raise DocumentRefused("the document is not valid JSON") from None
    if not isinstance(value, dict):
        raise DocumentRefused("the document is not a JSON object")
    return value


def render_view(payload: str) -> bytes:
    """The bytes `services` sees for a row: its JSON, indented two spaces, with a final newline."""
    return _utf8(_dump(_parse(payload), indent=2) + "\n")


def compact_payload(view: bytes) -> str:
    """The row text for a view: the app's own compact JSON (no spaces), keys in the view's order."""
    try:
        text = view.decode("utf-8")
    except UnicodeDecodeError:
        raise DocumentRefused("the document is not UTF-8") from None
    return _dump(_parse(text), separators=(",", ":"))


def same_value(current: str, proposed: str) -> bool:
    """True when two payload texts hold the same JSON value, however they are spaced or ordered numerically.

    Compared through a canonical rendering, not `==`: Python treats `True == 1` and `False == 0`, and a flag the user
    flipped would then be read as no change and never written.
    """
    return _canonical(_parse(current)) == _canonical(_parse(proposed))


def _canonical(value: dict[str, Any]) -> str:
    return _dump(value, sort_keys=True, separators=(",", ":"))


CANNOT_TELL: Final = "the document could not be read to tell whether the app manages it itself, so it is left alone"


def skip_reason(source: DocumentSource, payload: str) -> str | None:
    """Why this machine must leave `source` alone given its current `payload`, or `None` to go on.

    Only an unambiguous *off* lets the document be handled: an absent key is the app's default (off), `False` is off,
    and everything else — `True`, a value of another type, a payload that is not a property list — is on or cannot be
    told, which is skipped. Two writers on one document is the harm; a skipped cycle costs nothing.
    """
    if source.skip_when == "":
        return None
    try:
        document = parse_plist(payload.encode("utf-8"))
    except ValueError:
        return CANNOT_TELL
    value = document.get(source.skip_when, False)
    if value is False:
        return None
    return source.skip_because if value is True else CANNOT_TELL


def _plist_view(payload: str) -> bytes:
    return _plist(payload.encode("utf-8"))


def _plist_payload(view: bytes) -> str:
    return _plist(view).decode("utf-8")


def _plist_same(current: str, proposed: str) -> bool:
    return _plist_view(current) == _plist_view(proposed)


def _plist(data: bytes) -> bytes:
    try:
        return canonical_plist(data)
    except ValueError:
        raise DocumentRefused("the document is not a property list this build accepts") from None


@dataclass(frozen=True)
class DocumentCodec:
    """How one `kind` of document turns a stored payload into a view, back, and compares two payloads."""

    render: Callable[[str], bytes]
    compact: Callable[[bytes], str]
    same: Callable[[str, str], bool]


_CODECS: Final = {
    ORCA_PROFILE_STATE: DocumentCodec(render=render_view, compact=compact_payload, same=same_value),
    CFPREFS_DOMAIN: DocumentCodec(render=_plist_view, compact=_plist_payload, same=_plist_same),
}


def codec_for(kind: str) -> DocumentCodec:
    """The codec for a source's `kind`, or `DocumentRefused` for a kind no codec understands."""
    codec = _CODECS.get(kind)
    if codec is None:
        raise DocumentRefused("no document codec understands this kind of source")
    return codec


__all__ = [
    "CANNOT_TELL",
    "CFPREFS_DOMAIN",
    "ORCA_PROFILE_STATE",
    "DocumentCodec",
    "DocumentRead",
    "DocumentSource",
    "codec_for",
    "compact_payload",
    "content_hash",
    "render_view",
    "same_value",
    "skip_reason",
]
