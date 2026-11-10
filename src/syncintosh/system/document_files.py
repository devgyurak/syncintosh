# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `LiveFiles` that answers a database row's virtual path from a `DocumentStore`, the rest from the filesystem.

`services` moves bytes between a live path and an object and does not know that a few of those paths are not
files (`core/documents.py`). This router sits in front of the real `LiveFiles`: a path equal to a source's
`$HOME` + view path is read from, written to, and asked about through the store; any other path goes to the inner
`LiveFiles` unchanged, so no sync flow changes.

* `read` returns the row as indented JSON, and remembers the revision it saw for this path. `write` takes the
  bytes, compacts them, and hands the store that revision: a write with no read before it is refused, because it
  has no revision to check and would otherwise land on a row someone else just changed.
* The view's folder is virtual and exists exactly when the database does, so `apply`'s "is the app's folder here"
  question has the right answer. A database that is not there is never created.
* `remove` of a virtual path is refused: a row is never deleted, whatever a record says.

Only the revision map is state, and it holds integers. The constructor does no I/O (``AGENTS.md`` §2.6).
"""

from __future__ import annotations

from pathlib import Path

from syncintosh.core.documents import DocumentSource, compact_payload, render_view
from syncintosh.core.errors import DocumentRefused
from syncintosh.core.ports import DocumentStore, LiveFiles


class DocumentLiveFiles:
    """Route virtual document paths to a `DocumentStore`; everything else to `inner`."""

    def __init__(self, inner: LiveFiles, store: DocumentStore, home: str, sources: tuple[DocumentSource, ...]) -> None:
        self._inner = inner
        self._store = store
        self._views = {str(Path(home) / source.view): source for source in sources}
        self._directories = {str(Path(home) / source.view_directory): source for source in sources}
        self._seen: dict[str, int] = {}

    def read(self, path: str) -> bytes | None:
        source = self._views.get(self._key(path))
        if source is None:
            return self._inner.read(path)
        read = self._store.read(source)
        if read is None:
            self._seen.pop(source.view, None)
            return None
        self._seen[source.view] = read.revision
        return render_view(read.payload)

    def write(self, path: str, data: bytes, *, private: bool = False) -> None:
        source = self._views.get(self._key(path))
        if source is None:
            self._inner.write(path, data, private=private)
            return
        revision = self._seen.get(source.view)
        if revision is None:
            raise DocumentRefused("the document was not read first, so there is no revision to check")
        try:
            self._store.write(source, compact_payload(data), expected_revision=revision)
        finally:
            # One read licenses one write attempt, however it ended: the revision moved, the write was a no-op, or
            # the store refused; the next attempt must read again.
            self._seen.pop(source.view, None)

    def exists(self, path: str) -> bool:
        source = self._views.get(self._key(path))
        if source is not None:
            return self._store.present(source)
        directory = self._directories.get(self._key(path))
        if directory is not None:
            return self._store.present(directory)
        return self._inner.exists(path)

    def is_symlink(self, path: str) -> bool:
        source = self._views.get(self._key(path)) or self._directories.get(self._key(path))
        if source is not None:
            return self._store.is_symlink(source)
        return self._inner.is_symlink(path)

    def list_dir(self, path: str) -> tuple[str, ...]:
        return self._inner.list_dir(path)

    def remove(self, path: str) -> None:
        if self._key(path) in self._views:
            raise DocumentRefused("a document row is never deleted")
        self._inner.remove(path)

    @staticmethod
    def _key(path: str) -> str:
        return str(Path(path))


def open_live_files(inner: LiveFiles, home: str, sources: tuple[DocumentSource, ...]) -> LiveFiles:
    """The `LiveFiles` for a cycle: `inner` itself when no preset presents a document, else the router over it.

    `sqlite3` is imported here, not at module scope, so a machine with no such preset never loads it.
    """
    if not sources:
        return inner
    from syncintosh.system.sqlite_documents import SqliteDocumentStore

    return DocumentLiveFiles(inner, SqliteDocumentStore(home), home, sources)


__all__ = ["DocumentLiveFiles", "open_live_files"]
