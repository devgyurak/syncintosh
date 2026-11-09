# SPDX-License-Identifier: AGPL-3.0-or-later
"""A `DocumentStore` over an app's SQLite database: one `profile_state_documents` row, read and written the app's way.

Orca (1.4.x) keeps its settings as the `settings` row of `profiles/<id>/profile-state.db`; the JSON file it also
keeps is a stale compatibility export that it treats as an older build's edit when changed. This store reproduces
the app's own write protocol and refuses everything it was not written against. Evidence for the layout is in the
issue-55 Step 0 comment; no value was read.

**Fail closed.** The database is refused, with a fixed message, when:

* `PRAGMA user_version` is not 3, or a table or its columns are not the expected ones;
* `profile_state_meta.profile_id` is not the id of the folder the database sits in;
* the row is missing, its `domain_version` is not 1, its `revision` is above the profile's, or its `content_hash`
  is not the SHA-256 of its payload (the app treats a mismatch as a hard error too);
* the database or its folder is a symlink.

A database that does not exist is never created: `read` answers `None` and `write` refuses.

**Write protocol** (the app's own, so its next read succeeds): one `BEGIN IMMEDIATE` transaction that re-validates
the row, compares the profile revision with the one the caller read (`DocumentStale` when it moved), then updates
**only** that row and the meta `revision` to `revision + 1`, with the new hash and `updated_at` in milliseconds,
under `synchronous=FULL`. Other rows, `legacy_json_acceptance`, file permissions and the WAL files are not touched
by this code. The row is read back and verified after the commit. An outside write while the app runs makes it stop
saving, so callers defer while it is open (`Preset.defer_while_running`); the revision check is the backstop and
not a substitute.

Collect opens the database read-only (`mode=ro`), never `immutable=1`, which would miss what is still in the WAL.
The constructor does no I/O (``AGENTS.md`` §2.6), and only the CLI's wiring imports this module, so `sqlite3` stays
off the startup path.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from syncintosh.core.documents import (
    ORCA_PROFILE_STATE,
    DocumentRead,
    DocumentSource,
    content_hash,
    same_value,
)
from syncintosh.core.errors import DocumentRefused, DocumentStale, DocumentWrittenUnverified

_USER_VERSION = 3
_DOMAIN_VERSION = 1
_BUSY_TIMEOUT_MS = 5000

_META_COLUMNS = frozenset({"key", "value"})
_DOCUMENT_COLUMNS = frozenset({"domain", "payload", "domain_version", "revision", "updated_at", "content_hash"})


def _meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM profile_state_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None or not isinstance(row[0], str) else row[0]


class SqliteDocumentStore:
    """Reads and writes one document row; opens the database per call and keeps no connection."""

    def __init__(self, home: str) -> None:
        self._home = home

    def _database(self, source: DocumentSource) -> Path:
        return Path(self._home) / source.database

    def present(self, source: DocumentSource) -> bool:
        path = self._database(source)
        return path.is_symlink() or path.exists()

    def is_symlink(self, source: DocumentSource) -> bool:
        # Every component below home, not only the file and its folder: a linked `profiles/` would redirect the
        # whole path. Home itself is not judged (`/var` → `/private/var` is how macOS reaches a temp HOME).
        current = Path(self._home)
        for part in source.database.split("/"):
            current = current / part
            if current.is_symlink():
                return True
        return False

    def read(self, source: DocumentSource) -> DocumentRead | None:
        path = self._checked_path(source)
        if not path.exists():
            return None
        try:
            with closing(self._connect(path, "ro")) as connection:
                connection.execute("BEGIN")
                return self._validated(connection, source, path)
        except sqlite3.Error:
            raise DocumentRefused("the database could not be read") from None

    def write(self, source: DocumentSource, payload: str, *, expected_revision: int) -> None:
        path = self._checked_path(source)
        if not path.exists():
            # Never created: a database the app has not made is the app not having run.
            raise DocumentRefused("the database does not exist, so it is not created")
        try:
            with closing(self._connect(path, "rw")) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    changed = self._replace(connection, source, path, payload, expected_revision)
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT" if changed else "ROLLBACK")
        except sqlite3.Error:
            raise DocumentRefused("the database could not be written") from None
        if changed:
            self._verify(source, payload)

    def _checked_path(self, source: DocumentSource) -> Path:
        if source.kind != ORCA_PROFILE_STATE:
            raise DocumentRefused("no document store understands this kind of source")
        if self.is_symlink(source):
            raise DocumentRefused("the database is reached through a symlink")
        return self._database(source)

    @staticmethod
    def _connect(path: Path, mode: str) -> sqlite3.Connection:
        # `isolation_level=None` so the transaction is the explicit one the callers open, not Python's implicit one.
        connection = sqlite3.connect(
            f"file:{quote(str(path))}?mode={mode}", uri=True, isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000
        )
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if mode == "rw":
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _validated(connection: sqlite3.Connection, source: DocumentSource, path: Path) -> DocumentRead:
        """The row, after every check the app itself makes; raises `DocumentRefused` for any that fails."""
        if connection.execute("PRAGMA user_version").fetchone()[0] != _USER_VERSION:
            raise DocumentRefused("the database is a version this build does not support")
        meta_columns = frozenset(str(row[1]) for row in connection.execute("PRAGMA table_info(profile_state_meta)"))
        document_columns = frozenset(
            str(row[1]) for row in connection.execute("PRAGMA table_info(profile_state_documents)")
        )
        if meta_columns != _META_COLUMNS or document_columns != _DOCUMENT_COLUMNS:
            raise DocumentRefused("the database's tables are not the expected shape")
        if _meta(connection, "profile_id") != path.parent.name:
            raise DocumentRefused("the database belongs to a different profile than its folder")
        profile_revision = _meta(connection, "revision")
        if profile_revision is None or not profile_revision.isdecimal():
            raise DocumentRefused("the database has no usable revision")
        row = connection.execute(
            "SELECT payload, domain_version, revision, content_hash FROM profile_state_documents WHERE domain = ?",
            (source.domain,),
        ).fetchone()
        if row is None:
            raise DocumentRefused("the database has no such document")
        payload, domain_version, revision, stored_hash = row
        if not isinstance(payload, str) or not isinstance(revision, int) or isinstance(revision, bool):
            raise DocumentRefused("the document row is not the expected shape")
        if domain_version != _DOMAIN_VERSION:
            raise DocumentRefused("the document is a version this build does not support")
        if revision > int(profile_revision):
            raise DocumentRefused("the document is newer than its profile")
        if stored_hash != content_hash(payload):
            raise DocumentRefused("the document does not match its stored hash")
        return DocumentRead(payload=payload, revision=int(profile_revision))

    def _replace(
        self,
        connection: sqlite3.Connection,
        source: DocumentSource,
        path: Path,
        payload: str,
        expected_revision: int,
    ) -> bool:
        """Do the write inside the open transaction; True when there is something to commit."""
        current = self._validated(connection, source, path)
        if current.revision != expected_revision:
            raise DocumentStale("the document changed since it was read, so it was not written")
        if same_value(current.payload, payload):
            return False
        revision = current.revision + 1
        connection.execute(
            "UPDATE profile_state_documents SET payload = ?, domain_version = ?, revision = ?, updated_at = ?, "
            "content_hash = ? WHERE domain = ?",
            (payload, _DOMAIN_VERSION, revision, time.time_ns() // 1_000_000, content_hash(payload), source.domain),
        )
        connection.execute("UPDATE profile_state_meta SET value = ? WHERE key = 'revision'", (str(revision),))
        return True

    def _verify(self, source: DocumentSource, payload: str) -> None:
        """Read the row back: what the app will read is what was meant to be written."""
        try:
            read = self.read(source)
        except DocumentRefused:
            read = None
        if read is None or read.payload != payload:
            # After the commit the database has changed, so this is not a refusal to write: it is a write nobody
            # could confirm (`DocumentWrittenUnverified`).
            raise DocumentWrittenUnverified("the document was written but reading it back did not confirm it")


__all__ = ["SqliteDocumentStore"]
