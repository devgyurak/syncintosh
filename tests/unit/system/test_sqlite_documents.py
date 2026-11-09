# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SQLite document store against fixture databases built in `tmp_path` (never a real user database).

Covers the app's write protocol (hash, revisions, only one row touched) and every fail-closed boundary. The
payload keys are TEST-ONLY stand-ins.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from syncintosh.core.documents import ORCA_PROFILE_STATE, DocumentSource, compact_payload, render_view
from syncintosh.core.errors import DocumentRefused, DocumentStale
from syncintosh.system.sqlite_documents import SqliteDocumentStore

DATABASE = "app/profiles/main/state.db"
SOURCE = DocumentSource.create(
    kind=ORCA_PROFILE_STATE, database=DATABASE, domain="settings", view="app/profiles/main/@state/settings.json"
)
PAYLOAD = '{"theme":"dark","fontSize":13}'


def _store(tmp_path: Path) -> SqliteDocumentStore:
    return SqliteDocumentStore(str(tmp_path))


def _database(tmp_path: Path) -> Path:
    return tmp_path / DATABASE


def _query(database: Path, sql: str, *args: object) -> list[tuple]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, args).fetchall()


def _snapshot(database: Path) -> list[tuple]:
    """Everything in the database a write must leave alone, plus what it may change, in one comparable value."""
    return [
        *_query(database, "SELECT * FROM profile_state_meta ORDER BY key"),
        *_query(database, "SELECT * FROM profile_state_documents ORDER BY domain"),
    ]


# --------------------------------------------------------------------------- reading


def test_read_returns_the_payload_and_the_profile_revision(tmp_path, profile_state_db) -> None:
    # given
    profile_state_db(_database(tmp_path), revision=7)

    # when
    read = _store(tmp_path).read(SOURCE)

    # then
    assert read is not None
    assert (read.payload, read.revision) == (PAYLOAD, 7)


def test_read_of_a_missing_database_is_none_and_creates_nothing(tmp_path) -> None:
    # when
    read = _store(tmp_path).read(SOURCE)

    # then
    assert read is None
    assert (tmp_path / "app").exists() is False


def test_read_leaves_the_database_exactly_as_it_was(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(_database(tmp_path))
    before = _snapshot(database)

    # when
    _store(tmp_path).read(SOURCE)

    # then
    assert _snapshot(database) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_version": 2},
        {"user_version": 4},
        {"profile_id": "other"},
        {"domain_version": 2},
        {"with_settings_row": False},
        {"stored_hash": "0" * 64},
        {"row_revision": 99},
    ],
)
def test_an_unsupported_database_is_refused_for_reading(tmp_path, profile_state_db, overrides: dict) -> None:
    # given
    profile_state_db(_database(tmp_path), **overrides)

    # when & then
    with pytest.raises(DocumentRefused) as raised:
        _store(tmp_path).read(SOURCE)
    assert "dark" not in str(raised.value)


def test_a_tampered_payload_fails_the_stored_hash(tmp_path, profile_state_db) -> None:
    # given — the row's text changed without the hash
    database = profile_state_db(_database(tmp_path))
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE profile_state_documents SET payload = '{\"theme\":\"light\"}' WHERE domain='settings'"
        )
        connection.commit()

    # when & then
    with pytest.raises(DocumentRefused, match="hash"):
        _store(tmp_path).read(SOURCE)


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE profile_state_documents ADD COLUMN extra TEXT",
        "DROP TABLE profile_state_meta",
        "DELETE FROM profile_state_meta WHERE key = 'revision'",
        "UPDATE profile_state_meta SET value = 'x' WHERE key = 'revision'",
    ],
)
def test_a_changed_table_shape_or_revision_is_refused(tmp_path, profile_state_db, statement: str) -> None:
    # given
    database = profile_state_db(_database(tmp_path))
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(statement)
        connection.commit()

    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path).read(SOURCE)


def test_a_file_that_is_not_a_database_is_refused(tmp_path) -> None:
    # given
    _database(tmp_path).parent.mkdir(parents=True)
    _database(tmp_path).write_bytes(b"this is not sqlite" * 20)

    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path).read(SOURCE)


def test_a_source_of_an_unknown_kind_is_refused(tmp_path, profile_state_db) -> None:
    # given
    profile_state_db(_database(tmp_path))
    source = DocumentSource.create(kind="other", database=DATABASE, domain="settings", view=SOURCE.view)

    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path).read(source)


def test_a_database_behind_a_symlink_is_refused_for_reading_and_writing(tmp_path, profile_state_db) -> None:
    # given — the real database lives elsewhere; the profile's path is a link to it
    real = profile_state_db(tmp_path / "elsewhere" / "main" / "state.db")
    link = _database(tmp_path)
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    store = _store(tmp_path)

    # when & then
    assert store.is_symlink(SOURCE) is True
    with pytest.raises(DocumentRefused, match="symlink"):
        store.read(SOURCE)
    with pytest.raises(DocumentRefused, match="symlink"):
        store.write(SOURCE, '{"a":1}', expected_revision=7)


def test_a_symlinked_profile_folder_is_a_symlink_too(tmp_path, profile_state_db) -> None:
    # given
    profile_state_db(tmp_path / "elsewhere" / "main" / "state.db")
    (tmp_path / "app" / "profiles").mkdir(parents=True)
    (tmp_path / "app" / "profiles" / "main").symlink_to(tmp_path / "elsewhere" / "main")

    # when
    linked = _store(tmp_path).is_symlink(SOURCE)

    # then
    assert linked is True


@pytest.mark.parametrize("link", ["app", "app/profiles"])
def test_a_symlink_anywhere_below_home_is_refused_for_reading_and_writing(
    tmp_path, profile_state_db, link: str
) -> None:
    # given — an ancestor of the database is the link, the profile folder and file below it are real
    real = tmp_path / "elsewhere"
    profile_state_db(real / "profiles" / "main" / "state.db" if link == "app" else real / "main" / "state.db")
    (tmp_path / link).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / link).symlink_to(real)
    store = _store(tmp_path)

    # when & then
    assert store.is_symlink(SOURCE) is True
    with pytest.raises(DocumentRefused, match="symlink"):
        store.read(SOURCE)
    with pytest.raises(DocumentRefused, match="symlink"):
        store.write(SOURCE, '{"a":1}', expected_revision=7)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('{"on":true}', '{"on":1}'),
        ('{"on":1}', '{"on":true}'),
        ('{"on":false}', '{"on":0}'),
        ('{"n":{"on":false}}', '{"n":{"on":0}}'),
    ],
)
def test_a_flip_between_a_boolean_and_a_number_is_written(tmp_path, profile_state_db, before: str, after: str) -> None:
    # given
    database = profile_state_db(_database(tmp_path), settings=before)

    # when
    _store(tmp_path).write(SOURCE, after, expected_revision=7)

    # then
    assert _query(database, "SELECT payload, revision FROM profile_state_documents WHERE domain='settings'") == [
        (after, 8)
    ]


def test_present_is_true_when_the_database_file_exists(tmp_path, profile_state_db) -> None:
    # given
    profile_state_db(_database(tmp_path))

    # when
    found = _store(tmp_path).present(SOURCE)

    # then
    assert found is True


def test_present_is_false_when_there_is_no_database(tmp_path) -> None:
    # when
    found = _store(tmp_path).present(SOURCE)

    # then
    assert found is False


# --------------------------------------------------------------------------- writing


def test_a_write_follows_the_apps_protocol_and_touches_only_that_row_and_the_revision(
    tmp_path, profile_state_db
) -> None:
    # given
    database = profile_state_db(_database(tmp_path), revision=7)
    before = _snapshot(database)
    new = '{"theme":"light","fontSize":14}'

    # when
    _store(tmp_path).write(SOURCE, new, expected_revision=7)

    # then
    row = _query(
        database,
        "SELECT payload, domain_version, revision, content_hash FROM profile_state_documents WHERE domain='settings'",
    )
    meta = dict(_query(database, "SELECT key, value FROM profile_state_meta"))
    assert row == [(new, 1, 8, hashlib.sha256(new.encode()).hexdigest())]
    assert meta["revision"] == "8"
    untouched = [
        entry
        for entry in _snapshot(database)
        if entry[0] == "ui" or entry[0] in ("profile_id", "legacy_json_acceptance")
    ]
    assert untouched == [
        entry for entry in before if entry[0] == "ui" or entry[0] in ("profile_id", "legacy_json_acceptance")
    ]


def test_what_a_write_leaves_is_readable_by_the_next_read(tmp_path, profile_state_db) -> None:
    # given
    profile_state_db(_database(tmp_path), revision=7)
    store = _store(tmp_path)

    # when
    store.write(SOURCE, '{"a":1}', expected_revision=7)
    read = store.read(SOURCE)

    # then
    assert read is not None
    assert (read.payload, read.revision) == ('{"a":1}', 8)


def test_updated_at_is_a_millisecond_epoch(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(_database(tmp_path))

    # when
    _store(tmp_path).write(SOURCE, '{"a":1}', expected_revision=7)

    # then
    (stamp,) = _query(database, "SELECT updated_at FROM profile_state_documents WHERE domain='settings'")[0]
    assert 10**12 < stamp < 10**14


@pytest.mark.parametrize("same", ['{"theme":"dark","fontSize":13}', '{ "theme": "dark", "fontSize": 13.0 }'])
def test_a_write_of_an_equal_value_writes_nothing_and_keeps_the_revision(tmp_path, profile_state_db, same: str) -> None:
    # given
    database = profile_state_db(_database(tmp_path))
    before = _snapshot(database)

    # when
    _store(tmp_path).write(SOURCE, same, expected_revision=7)

    # then
    assert _snapshot(database) == before


def test_a_write_after_someone_else_moved_the_revision_fails_and_writes_nothing(tmp_path, profile_state_db) -> None:
    # given — collect saw revision 7; then the app saved (revision 8)
    database = profile_state_db(_database(tmp_path), revision=7)
    store = _store(tmp_path)
    seen = store.read(SOURCE)
    other = '{"theme":"light"}'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE profile_state_documents SET payload=?, revision=8, content_hash=? WHERE domain='settings'",
            (other, hashlib.sha256(other.encode()).hexdigest()),
        )
        connection.execute("UPDATE profile_state_meta SET value='8' WHERE key='revision'")
        connection.commit()
    before = _snapshot(database)

    # when & then
    assert seen is not None
    with pytest.raises(DocumentStale):
        store.write(SOURCE, '{"a":1}', expected_revision=seen.revision)
    assert _snapshot(database) == before


def test_a_write_to_a_missing_database_refuses_and_does_not_create_it(tmp_path) -> None:
    # when & then
    with pytest.raises(DocumentRefused, match="not created"):
        _store(tmp_path).write(SOURCE, '{"a":1}', expected_revision=7)
    assert (tmp_path / "app").exists() is False


@pytest.mark.parametrize(
    "overrides", [{"user_version": 4}, {"profile_id": "other"}, {"domain_version": 2}, {"stored_hash": "1" * 64}]
)
def test_a_write_to_an_unsupported_database_is_refused_and_leaves_it_alone(
    tmp_path, profile_state_db, overrides: dict
) -> None:
    # given
    database = profile_state_db(_database(tmp_path), **overrides)
    before = _snapshot(database)

    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path).write(SOURCE, '{"a":1}', expected_revision=7)
    assert _snapshot(database) == before


def test_a_write_keeps_the_files_mode(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(_database(tmp_path))
    database.chmod(0o600)

    # when
    _store(tmp_path).write(SOURCE, '{"a":1}', expected_revision=7)

    # then
    assert database.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("escape", ["\\ud83d", "\\ude00", "a\\ud83d\\ude00b"])
def test_a_payload_with_a_surrogate_escape_reads_writes_and_keeps_a_valid_hash(
    tmp_path, profile_state_db, escape: str
) -> None:
    # given — the app's own JSON, with the escape JS writes for an unpaired surrogate
    payload = f'{{"t":"{escape}"}}'
    database = profile_state_db(_database(tmp_path), settings=payload)
    store = _store(tmp_path)
    read = store.read(SOURCE)

    # when — the row is read, viewed, compacted and written back as a different value
    assert read is not None
    new = compact_payload(render_view(read.payload).replace(b'"t"', b'"u"'))
    store.write(SOURCE, new, expected_revision=read.revision)

    # then
    ((stored, stored_hash),) = _query(
        database, "SELECT payload, content_hash FROM profile_state_documents WHERE domain='settings'"
    )
    assert (stored, stored_hash) == (new, hashlib.sha256(new.encode("utf-8")).hexdigest())


def test_the_constructor_does_no_io(tmp_path) -> None:
    # when
    SqliteDocumentStore(str(tmp_path / "never-made"))

    # then
    assert (tmp_path / "never-made").exists() is False
