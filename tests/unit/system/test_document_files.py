# SPDX-License-Identifier: AGPL-3.0-or-later
"""The router that shows a database row as a virtual file, over a fake store and the real filesystem."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from syncintosh.core.documents import CFPREFS_DOMAIN, ORCA_PROFILE_STATE, DocumentRead, DocumentSource
from syncintosh.core.errors import DocumentRefused, DocumentStale, DocumentWrittenUnverified
from syncintosh.system.document_files import DocumentLiveFiles, open_live_files
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.sqlite_documents import SqliteDocumentStore

SOURCE = DocumentSource.create(
    kind=ORCA_PROFILE_STATE,
    database="app/profiles/main/state.db",
    domain="settings",
    view="app/profiles/main/@state/settings.json",
)


class FakeStore:
    """A `DocumentStore` over one in-memory row; records every call."""

    def __init__(
        self,
        payload: str | None = '{"a":1}',
        revision: str = "4",
        *,
        symlink: bool = False,
        write_error: Exception | None = None,
    ) -> None:
        self.write_error = write_error
        self.payload = payload
        self.revision = revision
        self.symlink = symlink
        self.writes: list[tuple[str, str]] = []

    def present(self, source: DocumentSource) -> bool:
        return self.payload is not None

    def is_symlink(self, source: DocumentSource) -> bool:
        return self.symlink

    def read(self, source: DocumentSource) -> DocumentRead | None:
        return None if self.payload is None else DocumentRead(payload=self.payload, revision=self.revision)

    def write(self, source: DocumentSource, payload: str, *, expected_revision: str) -> None:
        self.writes.append((payload, expected_revision))
        if self.write_error is not None:
            raise self.write_error


def _router(tmp_path: Path, store: FakeStore) -> DocumentLiveFiles:
    return DocumentLiveFiles(SystemLiveFiles(), {ORCA_PROFILE_STATE: store}, str(tmp_path), (SOURCE,))


def _view(tmp_path: Path) -> str:
    return str(tmp_path / SOURCE.view)


def test_the_virtual_path_reads_as_indented_json(tmp_path) -> None:
    # given
    live = _router(tmp_path, FakeStore('{"a":1,"b":{"c":2}}'))

    # when
    data = live.read(_view(tmp_path))

    # then
    assert data == b'{\n  "a": 1,\n  "b": {\n    "c": 2\n  }\n}\n'


def test_a_missing_database_reads_as_no_file(tmp_path) -> None:
    # when
    data = _router(tmp_path, FakeStore(None)).read(_view(tmp_path))

    # then
    assert data is None


def test_a_write_goes_to_the_store_compacted_with_the_revision_read_before_it(tmp_path) -> None:
    # given
    store = FakeStore(revision="9")
    live = _router(tmp_path, store)
    live.read(_view(tmp_path))

    # when
    live.write(_view(tmp_path), b'{\n  "a": 2\n}\n')

    # then
    assert store.writes == [('{"a":2}', "9")]


def test_a_write_with_no_read_before_it_is_refused(tmp_path) -> None:
    # given
    store = FakeStore()

    # when & then
    with pytest.raises(DocumentRefused, match="read first"):
        _router(tmp_path, store).write(_view(tmp_path), b'{"a":2}')
    assert store.writes == []


@pytest.mark.parametrize("error", [DocumentRefused("no"), DocumentStale("moved"), DocumentWrittenUnverified("?")])
def test_one_read_licenses_one_write_attempt_even_when_the_store_raises(tmp_path, error: Exception) -> None:
    # given
    store = FakeStore(write_error=error)
    live = _router(tmp_path, store)
    live.read(_view(tmp_path))
    with pytest.raises(type(error)):
        live.write(_view(tmp_path), b'{"a":2}')

    # when & then — the second attempt has no revision to check, so it never reaches the store
    with pytest.raises(DocumentRefused, match="read first"):
        live.write(_view(tmp_path), b'{"a":3}')
    assert len(store.writes) == 1


def test_one_read_licenses_one_write(tmp_path) -> None:
    # given
    store = FakeStore()
    live = _router(tmp_path, store)
    live.read(_view(tmp_path))
    live.write(_view(tmp_path), b'{"a":2}')

    # when & then — the revision moved, so a second write needs a fresh read
    with pytest.raises(DocumentRefused):
        live.write(_view(tmp_path), b'{"a":3}')


def test_a_write_that_is_not_one_json_object_never_reaches_the_store(tmp_path) -> None:
    # given
    store = FakeStore()
    live = _router(tmp_path, store)
    live.read(_view(tmp_path))

    # when & then
    with pytest.raises(DocumentRefused):
        live.write(_view(tmp_path), b"[1]")
    assert store.writes == []


def test_a_read_that_finds_no_database_forgets_the_revision(tmp_path) -> None:
    # given
    store = FakeStore()
    live = _router(tmp_path, store)
    live.read(_view(tmp_path))
    store.payload = None
    live.read(_view(tmp_path))

    # when & then
    with pytest.raises(DocumentRefused):
        live.write(_view(tmp_path), b'{"a":2}')


@pytest.mark.parametrize("payload", ['{"a":1}', None])
def test_the_virtual_file_and_its_folder_exist_exactly_when_the_database_does(tmp_path, payload: str | None) -> None:
    # given
    live = _router(tmp_path, FakeStore(payload))

    # when
    answers = (live.exists(_view(tmp_path)), live.exists(str(tmp_path / SOURCE.view_directory)))

    # then
    assert answers == (payload is not None, payload is not None)


@pytest.mark.parametrize("path", [SOURCE.view, SOURCE.view_directory])
def test_the_symlink_question_is_the_stores(tmp_path, path: str) -> None:
    # given
    live = _router(tmp_path, FakeStore(symlink=True))

    # when
    linked = live.is_symlink(str(tmp_path / path))

    # then
    assert linked is True


def test_a_document_row_is_never_removed(tmp_path) -> None:
    # when & then
    with pytest.raises(DocumentRefused, match="never deleted"):
        _router(tmp_path, FakeStore()).remove(_view(tmp_path))


def test_every_other_path_goes_to_the_real_filesystem(tmp_path) -> None:
    # given
    live = _router(tmp_path, FakeStore())
    other = tmp_path / "plain" / "file.txt"

    # when
    live.write(str(other), b"hello")

    # then
    answers = (
        live.read(str(other)),
        live.exists(str(other)),
        live.is_symlink(str(other)),
        live.list_dir(str(other.parent)),
    )
    live.remove(str(other))
    assert (answers, other.exists()) == ((b"hello", True, False, ("file.txt",)), False)


def test_open_live_files_is_the_inner_files_when_no_preset_has_a_document(tmp_path) -> None:
    # given
    inner = SystemLiveFiles()

    # when
    live = open_live_files(inner, str(tmp_path), ())

    # then
    assert live is inner


def test_open_live_files_routes_through_the_sqlite_store_when_a_preset_has_one(tmp_path, profile_state_db) -> None:
    # given
    profile_state_db(tmp_path / SOURCE.database, settings={"a": 1})
    live = open_live_files(SystemLiveFiles(), str(tmp_path), (SOURCE,))

    # when
    live.read(_view(tmp_path))
    live.write(_view(tmp_path), json.dumps({"a": 2}).encode())

    # then
    read = SqliteDocumentStore(str(tmp_path)).read(SOURCE)
    assert read is not None
    assert read.payload == '{"a":2}'


DOMAIN_SOURCE = DocumentSource.create(
    kind=CFPREFS_DOMAIN,
    database="Library/Preferences/com.example.app.plist",
    domain="com.example.app",
    view="Library/Preferences/@defaults/com.example.app.plist",
)


def test_each_source_is_served_by_the_store_of_its_kind(tmp_path) -> None:
    # given
    orca, prefs = FakeStore('{"a":1}', "1"), FakeStore(plistlib.dumps({"b": 2}).decode(), "2")
    live = DocumentLiveFiles(
        SystemLiveFiles(), {ORCA_PROFILE_STATE: orca, CFPREFS_DOMAIN: prefs}, str(tmp_path), (SOURCE, DOMAIN_SOURCE)
    )

    # when
    views = (live.read(_view(tmp_path)), live.read(str(tmp_path / DOMAIN_SOURCE.view)))
    live.write(str(tmp_path / DOMAIN_SOURCE.view), views[1] or b"")

    # then — JSON for the row, canonical plist XML for the domain, and the write went to the domain's store only
    assert views[0] == b'{\n  "a": 1\n}\n'
    assert (b"<key>b</key>" in (views[1] or b""), len(prefs.writes), orca.writes) == (True, 1, [])
    assert prefs.writes[0][1] == "2"


def test_a_source_whose_kind_has_no_store_is_refused(tmp_path) -> None:
    # given
    live = DocumentLiveFiles(SystemLiveFiles(), {}, str(tmp_path), (SOURCE,))

    # when & then
    with pytest.raises(DocumentRefused):
        live.read(_view(tmp_path))


def test_open_live_files_builds_only_the_stores_the_sources_need(tmp_path, monkeypatch, fake_defaults) -> None:
    # given — the fake stands in for the real tool, and the domain's plist is the temp home's
    monkeypatch.setattr("syncintosh.system.defaults_domains.DEFAULTS_EXECUTABLE", fake_defaults.path)
    home = tmp_path / "home"
    fake_defaults.seed(home, "com.example.app", {"a": 1})
    live = open_live_files(SystemLiveFiles(), str(home), (DOMAIN_SOURCE,))
    view = str(home / DOMAIN_SOURCE.view)

    # when
    before = live.read(view)
    live.write(view, (before or b"").replace(b"<integer>1</integer>", b"<integer>2</integer>"))

    # then — read, check, import, verify through the real subprocess path, and the plist itself is never opened by us
    assert (live.exists(view), fake_defaults.values(home, "com.example.app")) == (True, {"a": 2})
    assert [call["argv"][0] for call in fake_defaults.calls(home)] == ["export", "export", "import", "export"]


def test_a_domain_that_is_not_there_is_absent_and_its_folder_with_it(tmp_path) -> None:
    # given
    live = open_live_files(SystemLiveFiles(), str(tmp_path / "home"), (DOMAIN_SOURCE,))

    # when
    answers = (
        live.read(str(tmp_path / "home" / DOMAIN_SOURCE.view)),
        live.exists(str(tmp_path / "home" / DOMAIN_SOURCE.view)),
        live.exists(str(tmp_path / "home" / DOMAIN_SOURCE.view_directory)),
    )

    # then — no tool is run to find out: the plist file is what is asked
    assert answers == (None, False, False)
