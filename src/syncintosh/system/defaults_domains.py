# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `DocumentStore` over a macOS preferences domain, read and written through `defaults`, never through the plist.

`cfprefsd` owns a domain: it caches the values and rewrites the plist file on its own schedule, so writing the file
under it is lost or, worse, half-applied. `defaults` is the one supported door, and this store uses nothing else.
What was observed on a scratch domain (macOS 26, the issue-58 Step 0 comment; iTerm2's own domain was never written):

* `defaults export <domain> -` prints an XML plist and exits 0 — **also for a domain that does not exist**, where it
  prints an empty `<dict/>`. "Is the domain here" is therefore asked of the plist file, not of the export.
* `defaults import <domain> -` reads a plist from stdin and **merges per top-level key**: a key the input lacks is
  kept, a nested dict under a key the input has is replaced whole. Malformed input exits 1 and changes nothing.
* `defaults delete <domain> <key>` removes one top-level key.
* The file on disk is a binary plist, so it is never parsed here.

A write is therefore `import` of the whole document, then `delete` of every top-level key the new document no longer
has (so the domain ends up equal to the document), then a re-export that must equal what was meant
(`DocumentWrittenUnverified`). Import runs first: if it fails nothing changed, and a failure after it is reported as
an unverified write instead of a refusal because the domain already moved.

**Revision** is the SHA-256 of the canonical form of an export. A write exports again right before it changes
anything and refuses (`DocumentStale`) when that digest is not the one the caller read, so an app that wrote in
between is never overwritten. A document equal to the current one is a no-op.

**Process hygiene.** The tool is run by absolute path, with no shell, a fixed small environment and a timeout; the
domain is shape-checked first, and it comes from the local built-in catalog and never from a record. Every failure
is a fixed message: stderr can carry a value and is dropped. The constructor does no I/O (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Final

from syncintosh.core.documents import CFPREFS_DOMAIN, DocumentRead, DocumentSource, codec_for
from syncintosh.core.errors import DocumentRefused, DocumentStale, DocumentWrittenUnverified
from syncintosh.core.plist_xml import parse_plist

DEFAULTS_EXECUTABLE: Final = "/usr/bin/defaults"
# Longer than a healthy export (milliseconds) and short enough that a wedged `cfprefsd` cannot hang a sync.
DEFAULTS_TIMEOUT_SECONDS: Final = 10.0

# A reverse-DNS domain, the same shape as a bundle ID. It is passed as one argv element, so this is about refusing a
# value that is not a domain at all (a leading `-` would read as an option), not about quoting.
_DOMAIN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")

_ENVIRONMENT: Final = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


class _Interrupted(DocumentRefused):
    """The tool started and did not finish normally: a timeout or a signal. For a write, the outcome is unknown."""


class DefaultsDomainStore:
    """Reads and writes one preferences domain; runs `defaults` per call and keeps nothing."""

    def __init__(
        self,
        home: str,
        *,
        executable: str | None = None,
        timeout_seconds: float = DEFAULTS_TIMEOUT_SECONDS,
    ) -> None:
        self._home = home
        self._executable = DEFAULTS_EXECUTABLE if executable is None else executable
        self._timeout = timeout_seconds

    def _plist(self, source: DocumentSource) -> Path:
        return Path(self._home) / source.database

    def present(self, source: DocumentSource) -> bool:
        path = self._plist(source)
        return path.is_symlink() or path.exists()

    def is_symlink(self, source: DocumentSource) -> bool:
        # Every component below home, as `SqliteDocumentStore` does: a linked `Library/` would redirect the plist.
        # Home itself is not judged (`/var` → `/private/var` is how macOS reaches a temp HOME).
        current = Path(self._home)
        for part in source.database.split("/"):
            current = current / part
            if current.is_symlink():
                return True
        return False

    def read(self, source: DocumentSource) -> DocumentRead | None:
        self._checked(source)
        if not self.present(source):
            return None
        payload = self._export(source)
        return DocumentRead(payload=payload, revision=self._revision(source, payload))

    def write(self, source: DocumentSource, payload: str, *, expected_revision: str) -> None:
        self._checked(source)
        if not self.present(source):
            # Never created: a domain `cfprefsd` has no file for is the app not having run.
            raise DocumentRefused("the preferences domain does not exist, so it is not created")
        codec = codec_for(source.kind)
        current = self._export(source)
        if self._revision(source, current) != expected_revision:
            raise DocumentStale("the document changed since it was read, so it was not written")
        if codec.same(current, payload):
            return
        removed = sorted(_top_level_keys(current) - _top_level_keys(payload))
        try:
            self._run(source, ["import", source.domain, "-"], stdin=payload.encode("utf-8"))
        except _Interrupted:
            # A timeout or a signal does not say whether `cfprefsd` took the document, unlike a plain rc 1, which the
            # probe showed changes nothing.
            raise DocumentWrittenUnverified(
                "the import was interrupted, so whether it took effect is unknown"
            ) from None
        try:
            for key in removed:
                self._run(source, ["delete", source.domain, key])
        except DocumentRefused:
            raise DocumentWrittenUnverified("the document was written but a removed key could not be deleted") from None
        self._verify(source, payload)

    def _checked(self, source: DocumentSource) -> None:
        if source.kind != CFPREFS_DOMAIN:
            raise DocumentRefused("no document store understands this kind of source")
        if _DOMAIN.fullmatch(source.domain) is None:
            raise DocumentRefused("the preferences domain is not a valid domain name")
        if self.is_symlink(source):
            raise DocumentRefused("the preferences plist is reached through a symlink")

    def _export(self, source: DocumentSource) -> str:
        try:
            return self._run(source, ["export", source.domain, "-"]).decode("utf-8")
        except UnicodeDecodeError:
            raise DocumentRefused("the preferences export is not UTF-8") from None

    def _run(self, source: DocumentSource, arguments: list[str], *, stdin: bytes | None = None) -> bytes:
        try:
            result = subprocess.run(
                [self._executable, *arguments],
                input=stdin,
                capture_output=True,
                timeout=self._timeout,
                check=False,
                env=_ENVIRONMENT | {"HOME": self._home},
                stdin=subprocess.DEVNULL if stdin is None else None,
            )
        except subprocess.TimeoutExpired:
            raise _Interrupted("the defaults tool did not answer in time") from None
        except (OSError, subprocess.SubprocessError):
            raise DocumentRefused("the defaults tool could not be run") from None
        if result.returncode < 0:
            raise _Interrupted("the defaults tool was stopped by a signal")
        if result.returncode != 0:
            raise DocumentRefused("the defaults tool refused the request")
        return result.stdout

    @staticmethod
    def _revision(source: DocumentSource, payload: str) -> str:
        # Hashed in canonical form, so a re-export that only differs in spacing is not a change.
        return hashlib.sha256(codec_for(source.kind).render(payload)).hexdigest()

    def _verify(self, source: DocumentSource, payload: str) -> None:
        """Export again: what `cfprefsd` now holds is what was meant to be written."""
        try:
            read_back = self._export(source)
            confirmed = codec_for(source.kind).same(read_back, payload)
        except DocumentRefused:
            confirmed = False
        if not confirmed:
            # After the import the domain has changed, so this is not a refusal to write: it is a write nobody could
            # confirm (`DocumentWrittenUnverified`).
            raise DocumentWrittenUnverified("the document was written but reading it back did not confirm it")


def _top_level_keys(payload: str) -> frozenset[str]:
    # Both documents passed the codec's `same` check first, which refuses anything `parse_plist` would, so this
    # cannot raise on the way `write` reaches it.
    return frozenset(parse_plist(payload.encode("utf-8")))


__all__ = ["DEFAULTS_EXECUTABLE", "DEFAULTS_TIMEOUT_SECONDS", "DefaultsDomainStore"]
