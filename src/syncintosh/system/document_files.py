# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `LiveFiles` that answers a document's virtual path from a `DocumentStore`, the rest from the filesystem.

`services` moves bytes between a live path and an object and does not know that a few of those paths are not
files (`core/documents.py`). This router sits in front of the real `LiveFiles`: a path equal to a source's
`$HOME` + view path is read from, written to, and asked about through the store **for that source's kind**; any
other path goes to the inner `LiveFiles` unchanged, so no sync flow changes.

* `read` returns the document in its kind's view (indented JSON for a row, canonical XML for a preferences
  domain), and remembers the revision it saw for this path. `write` takes the bytes, turns them back into the
  stored form, and hands the store that revision: a write with no read before it is refused, because it has no
  revision to check and would otherwise land on a document someone else just changed.
* The view's folder is virtual and exists exactly when the database or plist does, so `apply`'s "is the app's
  folder here" question has the right answer. A document that is not there is never created.
* `remove` of a virtual path is refused: a document is never deleted, whatever a record says.

Only the revision map is state, and it holds opaque tokens. The constructor does no I/O (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from syncintosh.core.documents import (
    CANNOT_TELL,
    CFPREFS_DOMAIN,
    ORCA_PROFILE_STATE,
    DocumentSource,
    codec_for,
    skip_reason,
)
from syncintosh.core.errors import DocumentRefused
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import DocumentSkips, DocumentStore, LiveFiles


class DocumentLiveFiles:
    """Route virtual document paths to a `DocumentStore`; everything else to `inner`."""

    def __init__(
        self,
        inner: LiveFiles,
        stores: Mapping[str, DocumentStore],
        home: str,
        sources: tuple[DocumentSource, ...],
    ) -> None:
        self._inner = inner
        self._stores = stores
        self._views = {str(Path(home) / source.view): source for source in sources}
        self._directories = {str(Path(home) / source.view_directory): source for source in sources}
        self._seen: dict[str, str] = {}

    def read(self, path: str) -> bytes | None:
        source = self._views.get(self._key(path))
        if source is None:
            return self._inner.read(path)
        read = self._store_for(source).read(source)
        if read is None:
            self._seen.pop(source.view, None)
            return None
        self._seen[source.view] = read.revision
        return codec_for(source.kind).render(read.payload)

    def write(self, path: str, data: bytes, *, private: bool = False) -> None:
        source = self._views.get(self._key(path))
        if source is None:
            self._inner.write(path, data, private=private)
            return
        revision = self._seen.get(source.view)
        if revision is None:
            raise DocumentRefused("the document was not read first, so there is no revision to check")
        try:
            self._store_for(source).write(source, codec_for(source.kind).compact(data), expected_revision=revision)
        finally:
            # One read licenses one write attempt, however it ended: the revision moved, the write was a no-op, or
            # the store refused; the next attempt must read again.
            self._seen.pop(source.view, None)

    def exists(self, path: str) -> bool:
        source = self._views.get(self._key(path))
        if source is not None:
            return self._store_for(source).present(source)
        directory = self._directories.get(self._key(path))
        if directory is not None:
            return self._store_for(directory).present(directory)
        return self._inner.exists(path)

    def is_symlink(self, path: str) -> bool:
        source = self._views.get(self._key(path)) or self._directories.get(self._key(path))
        if source is not None:
            return self._store_for(source).is_symlink(source)
        return self._inner.is_symlink(path)

    def list_dir(self, path: str) -> tuple[str, ...]:
        return self._inner.list_dir(path)

    def remove(self, path: str) -> None:
        if self._key(path) in self._views:
            raise DocumentRefused("a document row is never deleted")
        self._inner.remove(path)

    def _store_for(self, source: DocumentSource) -> DocumentStore:
        store = self._stores.get(source.kind)
        if store is None:
            raise DocumentRefused("no document store understands this kind of source")
        return store

    @staticmethod
    def _key(path: str) -> str:
        return str(Path(path))


def _stores(home: str, sources: tuple[DocumentSource, ...]) -> dict[str, DocumentStore]:
    """One store per kind of `sources`, each imported only when a source needs it."""
    stores: dict[str, DocumentStore] = {}
    kinds = {source.kind for source in sources}
    if ORCA_PROFILE_STATE in kinds:
        from syncintosh.system.sqlite_documents import SqliteDocumentStore

        stores[ORCA_PROFILE_STATE] = SqliteDocumentStore(home)
    if CFPREFS_DOMAIN in kinds:
        from syncintosh.system.defaults_domains import DefaultsDomainStore

        stores[CFPREFS_DOMAIN] = DefaultsDomainStore(home)
    return stores


def open_live_files(inner: LiveFiles, home: str, sources: tuple[DocumentSource, ...]) -> LiveFiles:
    """The `LiveFiles` for a cycle: `inner` itself when no preset presents a document, else the router over it.

    A store is built only for a kind a preset actually presents, and its module is imported here, not at module
    scope, so a machine with no such preset never loads `sqlite3` or `subprocess`-driven code it does not use.
    """
    if not sources:
        return inner
    return DocumentLiveFiles(inner, _stores(home, sources), home, sources)


class DocumentSkipCheck:
    """The `DocumentSkips` port: reads each document that can be skipped, once per call, and asks the core rule.

    A document with no plist or database here is not a skip: that is the app not being installed, which apply
    already defers. One that is there but cannot be read is skipped with `CANNOT_TELL`: two tools writing one
    document is the harm, and a skipped cycle costs nothing. The constructor does no I/O (``AGENTS.md`` §2.6).
    """

    def __init__(self, stores: Mapping[str, DocumentStore], sources: tuple[DocumentSource, ...]) -> None:
        self._stores = stores
        self._sources = tuple(source for source in sources if source.skip_when != "")

    def reasons(self, tracked: frozenset[str]) -> Mapping[str, str]:
        found: dict[str, str] = {}
        # Compared the way `LivePaths.resolve` compares, so a hand-typed path in another case still counts as tracked.
        tracked_keys = {collision_key(path) for path in tracked}
        for source in self._sources:
            if collision_key(source.view) not in tracked_keys:
                continue
            reason = self._reason(source)
            if reason is not None:
                found[source.view] = reason
        return found

    def _reason(self, source: DocumentSource) -> str | None:
        store = self._stores.get(source.kind)
        if store is None:
            return CANNOT_TELL
        try:
            if not store.present(source):
                return None
            read = store.read(source)
        except (DocumentRefused, OSError):
            # An unreadable folder is one document left alone, not a reason to stop every other preset's cycle.
            return CANNOT_TELL
        return None if read is None else skip_reason(source, read.payload)


def open_document_skips(home: str, sources: tuple[DocumentSource, ...]) -> DocumentSkips | None:
    """The skip check for a cycle, or `None` when no presented document can be skipped."""
    skippable = tuple(source for source in sources if source.skip_when != "")
    return DocumentSkipCheck(_stores(home, skippable), skippable) if skippable else None


__all__ = ["DocumentLiveFiles", "DocumentSkipCheck", "open_document_skips", "open_live_files"]
