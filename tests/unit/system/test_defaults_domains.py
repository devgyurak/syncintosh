# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preferences-domain store, driven through a fake `defaults`; the real tool and `cfprefsd` are never run."""

from __future__ import annotations

import plistlib
import stat

import pytest

from syncintosh.core.documents import CFPREFS_DOMAIN, ORCA_PROFILE_STATE, DocumentSource
from syncintosh.core.errors import DocumentRefused, DocumentStale, DocumentWrittenUnverified
from syncintosh.system.defaults_domains import DefaultsDomainStore

DOMAIN = "com.example.app"
SOURCE = DocumentSource.create(
    kind=CFPREFS_DOMAIN,
    database=f"Library/Preferences/{DOMAIN}.plist",
    domain=DOMAIN,
    view=f"Library/Preferences/@defaults/{DOMAIN}.plist",
)


def _xml(values: dict) -> str:
    return plistlib.dumps(values, fmt=plistlib.FMT_XML).decode("utf-8")


def _store(tmp_path, fake) -> DefaultsDomainStore:
    return DefaultsDomainStore(str(tmp_path / "home"), executable=fake.path)


def _home(tmp_path):
    return tmp_path / "home"


def test_the_real_tool_is_run_by_absolute_path(monkeypatch) -> None:
    # given — the suite-wide guard replaced it with a stub; look at the shipped value
    monkeypatch.undo()
    from syncintosh.system import defaults_domains

    # when & then
    assert defaults_domains.DEFAULTS_EXECUTABLE == "/usr/bin/defaults"


def test_the_constructor_runs_nothing(tmp_path, fake_defaults) -> None:
    # when
    DefaultsDomainStore(str(_home(tmp_path)), executable=fake_defaults.path)

    # then
    assert fake_defaults.calls(_home(tmp_path)) == []


def test_a_domain_with_no_plist_reads_as_absent_without_asking_defaults(tmp_path, fake_defaults) -> None:
    # when
    read = _store(tmp_path, fake_defaults).read(SOURCE)

    # then — an export of a missing domain is an empty dict with rc 0, so the plist file is what decides
    assert (read, fake_defaults.calls(_home(tmp_path))) == (None, [])


def test_a_present_domain_reads_as_its_export_with_a_digest_revision(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"b": 2, "a": 1})

    # when
    read = _store(tmp_path, fake_defaults).read(SOURCE)

    # then
    assert read is not None
    assert (plistlib.loads(read.payload.encode("utf-8")), len(read.revision)) == ({"a": 1, "b": 2}, 64)
    assert fake_defaults.calls(_home(tmp_path)) == [{"argv": ["export", DOMAIN, "-"], "stdin": ""}]


def test_the_revision_changes_with_the_content_and_not_with_a_reread(tmp_path, fake_defaults) -> None:
    # given
    store = _store(tmp_path, fake_defaults)
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    first, again = store.read(SOURCE), store.read(SOURCE)
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 2})

    # when
    changed = store.read(SOURCE)

    # then
    assert first is not None
    assert again is not None
    assert changed is not None
    assert (first.revision == again.revision, first.revision == changed.revision) == (True, False)


def test_a_plist_file_counts_as_present_even_as_a_dangling_symlink(tmp_path, fake_defaults) -> None:
    # given
    plist = _home(tmp_path) / SOURCE.database
    plist.parent.mkdir(parents=True)
    plist.symlink_to(tmp_path / "nowhere")

    # when & then
    assert _store(tmp_path, fake_defaults).present(SOURCE) is True


@pytest.mark.parametrize("linked", ["Library", "Library/Preferences", f"Library/Preferences/{DOMAIN}.plist"])
def test_a_symlink_anywhere_below_home_is_refused(tmp_path, fake_defaults, linked: str) -> None:
    # given
    real = tmp_path / "elsewhere"
    (real / "Library" / "Preferences").mkdir(parents=True)
    (real / "Library" / "Preferences" / f"{DOMAIN}.plist").write_bytes(plistlib.dumps({"a": 1}))
    link = _home(tmp_path) / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real / linked)
    store = _store(tmp_path, fake_defaults)

    # when & then
    with pytest.raises(DocumentRefused):
        store.read(SOURCE)
    assert (store.is_symlink(SOURCE), fake_defaults.calls(_home(tmp_path))) == (True, [])


def test_a_write_imports_the_document_then_deletes_the_keys_it_no_longer_has(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"gone": 1, "kept": 2, "nested": {"x": 1, "y": 2}})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    proposed = _xml({"kept": 3, "nested": {"x": 5}, "new": True})

    # when
    store.write(SOURCE, proposed, expected_revision=read.revision)

    # then — the domain ends up equal to the document, and the calls are exactly export, export, import, delete, export
    assert fake_defaults.values(_home(tmp_path), DOMAIN) == {"kept": 3, "nested": {"x": 5}, "new": True}
    assert [call["argv"] for call in fake_defaults.calls(_home(tmp_path))] == [
        ["export", DOMAIN, "-"],
        ["export", DOMAIN, "-"],
        ["import", DOMAIN, "-"],
        ["delete", DOMAIN, "gone"],
        ["export", DOMAIN, "-"],
    ]


def test_the_import_receives_the_document_on_stdin(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    proposed = _xml({"a": 2})

    # when
    store.write(SOURCE, proposed, expected_revision=read.revision)

    # then
    imports = [call for call in fake_defaults.calls(_home(tmp_path)) if call["argv"][0] == "import"]
    assert [call["stdin"] for call in imports] == [proposed]


def test_a_document_equal_to_the_current_one_writes_nothing(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1, "b": 1.0})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None

    # when — the same value in another key order
    store.write(SOURCE, _xml({"b": 1.0, "a": 1}), expected_revision=read.revision)

    # then
    verbs = [call["argv"][0] for call in fake_defaults.calls(_home(tmp_path))]
    assert verbs == ["export", "export"]


def test_a_domain_that_moved_since_the_read_is_stale_and_untouched(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 99})

    # when & then
    with pytest.raises(DocumentStale) as raised:
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)
    assert (
        isinstance(raised.value, DocumentWrittenUnverified),
        fake_defaults.values(_home(tmp_path), DOMAIN),
    ) == (False, {"a": 99})


def test_a_write_to_a_domain_with_no_plist_is_refused_and_creates_nothing(tmp_path, fake_defaults) -> None:
    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path, fake_defaults).write(SOURCE, _xml({"a": 1}), expected_revision="x")
    assert fake_defaults.calls(_home(tmp_path)) == []


def test_a_refused_import_is_a_plain_refusal_and_changes_nothing(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    fake_defaults.refuse(_home(tmp_path), "import")

    # when & then
    with pytest.raises(DocumentRefused) as raised:
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)
    assert (isinstance(raised.value, DocumentWrittenUnverified), fake_defaults.values(_home(tmp_path), DOMAIN)) == (
        False,
        {"a": 1},
    )


def test_a_delete_that_fails_after_the_import_is_the_unverified_signal(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"gone": 1, "a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    fake_defaults.refuse(_home(tmp_path), "delete")

    # when & then — the import already moved the domain, so this is not a refusal to write
    with pytest.raises(DocumentWrittenUnverified):
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)
    assert fake_defaults.values(_home(tmp_path), DOMAIN) == {"gone": 1, "a": 2}


def test_a_read_back_that_does_not_confirm_the_write_is_the_unverified_signal(tmp_path, fake_defaults) -> None:
    # given — an import that exits 0 and changes nothing
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    fake_defaults.drop_imports(_home(tmp_path))

    # when & then
    with pytest.raises(DocumentWrittenUnverified):
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)


def test_an_export_that_fails_during_the_read_back_is_the_unverified_signal(tmp_path, fake_defaults) -> None:
    # given — every export after the import is refused
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    read = _store(tmp_path, fake_defaults).read(SOURCE)
    assert read is not None
    script = tmp_path / "once"
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = export ] && [ -e "$HOME/.exported" ]; then exit 1; fi\n'
        f'if [ "$1" = export ]; then touch "$HOME/.exported"; fi\nexec {fake_defaults.path} "$@"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    store = DefaultsDomainStore(str(_home(tmp_path)), executable=str(script))

    # when & then — the first export is the stale check; the second one fails, and by then the import has happened
    with pytest.raises(DocumentWrittenUnverified):
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)


@pytest.mark.parametrize("domain", ["-flag", "a b", "a/b", "é.app", ".hidden", "a;rm"])
def test_a_domain_that_is_not_a_domain_name_never_reaches_the_tool(tmp_path, fake_defaults, domain: str) -> None:
    # given
    source = DocumentSource.create(
        kind=CFPREFS_DOMAIN,
        database=f"Library/Preferences/{domain}.plist",
        domain=domain,
        view="Library/Preferences/@defaults/x.plist",
    )
    fake_defaults.seed(_home(tmp_path), "x", {"a": 1})
    (_home(tmp_path) / source.database).parent.mkdir(parents=True, exist_ok=True)
    (_home(tmp_path) / source.database).write_bytes(plistlib.dumps({"a": 1}))

    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path, fake_defaults).read(source)
    assert fake_defaults.calls(_home(tmp_path)) == []


def test_a_source_of_another_kind_is_refused(tmp_path, fake_defaults) -> None:
    # given
    other = DocumentSource.create(
        kind=ORCA_PROFILE_STATE,
        database="Library/Preferences/x.plist",
        domain=DOMAIN,
        view="Library/Preferences/@defaults/x.plist",
    )

    # when & then
    with pytest.raises(DocumentRefused):
        _store(tmp_path, fake_defaults).read(other)


def test_a_tool_that_cannot_be_run_is_a_refusal(tmp_path) -> None:
    # given
    plist = _home(tmp_path) / SOURCE.database
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"a": 1}))
    store = DefaultsDomainStore(str(_home(tmp_path)), executable=str(tmp_path / "missing"))

    # when & then
    with pytest.raises(DocumentRefused):
        store.read(SOURCE)


def test_a_tool_that_outlives_the_timeout_is_a_refusal(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = DefaultsDomainStore(str(_home(tmp_path)), executable=fake_defaults.path, timeout_seconds=0.0001)

    # when & then
    with pytest.raises(DocumentRefused):
        store.read(SOURCE)


def test_an_export_that_is_not_utf8_is_a_refusal(tmp_path) -> None:
    # given
    plist = _home(tmp_path) / SOURCE.database
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"x")
    script = tmp_path / "bad"
    script.write_text("#!/bin/sh\nprintf '\\377'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    # when & then
    with pytest.raises(DocumentRefused):
        DefaultsDomainStore(str(_home(tmp_path)), executable=str(script)).read(SOURCE)


@pytest.mark.parametrize("payload", ["not a plist", '<?xml version="1.0"?><plist version="1.0"><array/></plist>'])
def test_a_document_that_is_not_a_plist_dictionary_is_refused_before_any_change(
    tmp_path, fake_defaults, payload: str
) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None

    # when & then
    with pytest.raises(DocumentRefused):
        store.write(SOURCE, payload, expected_revision=read.revision)
    assert fake_defaults.values(_home(tmp_path), DOMAIN) == {"a": 1}


def test_an_import_that_outlives_the_timeout_is_the_unverified_signal_not_a_refusal(
    tmp_path, fake_defaults, monkeypatch
) -> None:
    # given — exports answer; the import times out (the wrapper raises what `subprocess.run` raises, with no wait)
    import subprocess

    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    store = _store(tmp_path, fake_defaults)
    read = store.read(SOURCE)
    assert read is not None
    real_run = subprocess.run

    def run(command, **options):
        if command[1] == "import":
            raise subprocess.TimeoutExpired(command, 10.0)
        return real_run(command, **options)

    monkeypatch.setattr("syncintosh.system.defaults_domains.subprocess.run", run)

    # when & then
    with pytest.raises(DocumentWrittenUnverified):
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)


def test_an_import_killed_by_a_signal_is_the_unverified_signal(tmp_path, fake_defaults) -> None:
    # given
    fake_defaults.seed(_home(tmp_path), DOMAIN, {"a": 1})
    script = tmp_path / "killed-import"
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = import ]; then kill -9 $$; fi\nexec {fake_defaults.path} "$@"\n', encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    store = DefaultsDomainStore(str(_home(tmp_path)), executable=str(script))
    read = _store(tmp_path, fake_defaults).read(SOURCE)
    assert read is not None

    # when & then
    with pytest.raises(DocumentWrittenUnverified):
        store.write(SOURCE, _xml({"a": 2}), expected_revision=read.revision)
