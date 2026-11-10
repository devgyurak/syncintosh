# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the iTerm2 preferences domain: what travels, what never does, and when the domain is left alone.

The key names are iTerm2's own (`tests/fixtures/iterm2/registered_keys.txt` from `iTermPreferences.m` at v3.7.7, and
`observed_keys.txt` from a fresh install); no value is read or invented here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncintosh.core.documents import (
    CANNOT_TELL,
    CFPREFS_DOMAIN,
    ORCA_PROFILE_STATE,
    DocumentRead,
    DocumentSource,
    skip_reason,
)
from syncintosh.core.errors import ConfigError, DocumentRefused
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.iterm2 import (
    DATABASE,
    EXCLUDED_NAMES,
    ITERM2,
    LOCAL,
    LOCAL_PREFIXES,
    OBSERVED_LOCAL,
    SOURCE,
    SYNCED_NAMES,
    VIEW,
)
from syncintosh.services.profile_paths import LivePaths, Unresolved
from syncintosh.system.deny_list import is_denied
from syncintosh.system.document_files import DocumentSkipCheck, open_document_skips

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "iterm2"
REGISTERED = frozenset((FIXTURES / "registered_keys.txt").read_text(encoding="utf-8").splitlines())
OBSERVED = frozenset((FIXTURES / "observed_keys.txt").read_text(encoding="utf-8").splitlines())
EXTRA_SYNCED = {"New Bookmarks", "Default Bookmark Guid", "GlobalKeyMap"}
LOCAL_NAMES = frozenset(name for names in (*LOCAL.values(), *OBSERVED_LOCAL.values()) for name in names)

# iTerm2's own rule (`iTermRemotePreferencesKeyIsSyncable`, v3.7.7), copied as data so the preset is held to it.
ITERM2_EXEMPT_NAMES = frozenset(
    {
        "LoadPrefsFromCustomFolder",
        "PrefsCustomFolder",
        "Secure Input",
        "moveToApplicationsFolderAlertSuppress",
        "iTerm Version",
        "CGFontRenderingFontSmoothingDisabled",
        "PreventEscapeSequenceFromChangingProfile",
        "PreventEscapeSequenceFromClearingHistory",
        "Coprocess MRU",
        "MetalCaptureEnabled",
        "MetalCaptureEnabledDate",
        "Snippets",
    }
)
ITERM2_EXEMPT_PREFIXES = ("NS", "SU", "NoSync", "UK")


def _classified(key: str) -> bool:
    return key in SYNCED_NAMES or key in EXCLUDED_NAMES or key in LOCAL_NAMES or key.startswith(LOCAL_PREFIXES)


def test_every_registered_key_is_synced_excluded_or_local_with_a_reason() -> None:
    # when
    unclassified = sorted(key for key in REGISTERED if not _classified(key))

    # then
    assert unclassified == []


def test_every_key_seen_on_a_fresh_install_is_synced_excluded_or_local() -> None:
    # when
    unclassified = sorted(key for key in OBSERVED if not _classified(key))

    # then
    assert unclassified == []


def test_a_key_has_exactly_one_home() -> None:
    # when
    synced, excluded = set(SYNCED_NAMES), set(EXCLUDED_NAMES)
    homes = [synced & excluded, synced & LOCAL_NAMES, excluded & LOCAL_NAMES]

    # then
    assert homes == [set(), set(), set()]


def test_only_registered_keys_and_the_three_profile_keys_travel() -> None:
    # when
    strangers = sorted(set(SYNCED_NAMES) - REGISTERED - EXTRA_SYNCED)

    # then
    assert strangers == []


def test_nothing_iterm2_itself_never_syncs_is_synced_here() -> None:
    # when
    contradicted = sorted(
        key for key in SYNCED_NAMES if key in ITERM2_EXEMPT_NAMES or key.startswith(ITERM2_EXEMPT_PREFIXES)
    )

    # then
    assert contradicted == []


@pytest.mark.parametrize(
    "key",
    ["NoSyncOpenAIAPIKey", "NoUserDefaultAIAPIKey", "AICustomHeaders"],
)
def test_a_key_that_can_hold_a_secret_is_excluded_and_never_synced(key: str) -> None:
    # when & then
    assert (key in EXCLUDED_NAMES, key in SYNCED_NAMES) == (True, False)


def test_the_view_is_one_plist_file_with_the_allowlist_and_the_profile_array() -> None:
    # when
    file = next(each for each in ITERM2.files if each.path == VIEW)

    # then
    assert (file.format, file.synced is not None, list(file.array_rules)) == ("plist", True, [("New Bookmarks",)])


def test_the_preset_waits_for_the_app_and_presents_the_domain() -> None:
    # when & then
    assert (ITERM2.defer_while_running, ITERM2.documents) == (True, (SOURCE,))


def test_the_source_names_its_domain_and_a_view_no_real_folder_holds() -> None:
    # when & then
    assert (SOURCE.kind, SOURCE.database, SOURCE.domain, SOURCE.view) == (
        CFPREFS_DOMAIN,
        "Library/Preferences/com.googlecode.iterm2.plist",
        "com.googlecode.iterm2",
        "Library/Preferences/@defaults/com.googlecode.iterm2.plist",
    )


def test_the_catalog_lists_the_domain_and_every_cfprefs_plist_is_denied_while_its_view_is_not(tmp_path) -> None:
    # given
    home = tmp_path / "home"
    sources = [source for source in BuiltinCatalog.default().documents() if source.kind == CFPREFS_DOMAIN]

    # when — the real plist belongs to `cfprefsd`, so the only door is the virtual view
    verdicts = [(is_denied(home / source.database, home), is_denied(home / source.view, home)) for source in sources]

    # then
    assert (DATABASE in [source.database for source in sources], verdicts) == (True, [(True, False)] * len(sources))


# --------------------------------------------------------------------------- the skip rule


def _plist(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>{body}</dict></plist>'


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", None),
        ("<key>LoadPrefsFromCustomFolder</key><false/>", None),
        ("<key>LoadPrefsFromCustomFolder</key><true/>", SOURCE.skip_because),
        ("<key>LoadPrefsFromCustomFolder</key><integer>1</integer>", CANNOT_TELL),
        ("<key>LoadPrefsFromCustomFolder</key><string>YES</string>", CANNOT_TELL),
        ("<key>LoadPrefsFromCustomFolder</key><integer>0</integer>", CANNOT_TELL),
    ],
)
def test_only_an_absent_or_plainly_false_key_lets_the_domain_be_handled(body: str, expected: str | None) -> None:
    # when & then
    assert skip_reason(SOURCE, _plist(body)) == expected


@pytest.mark.parametrize("payload", ["", "not a plist", '<plist version="1.0"><array/></plist>'])
def test_a_payload_that_is_not_a_plist_dictionary_cannot_be_told_and_is_skipped(payload: str) -> None:
    # when & then
    assert skip_reason(SOURCE, payload) == CANNOT_TELL


def test_a_source_without_a_skip_rule_is_never_skipped() -> None:
    # given
    plain = DocumentSource.create(
        kind=CFPREFS_DOMAIN,
        database="Library/Preferences/com.example.app.plist",
        domain="com.example.app",
        view="Library/Preferences/@defaults/com.example.app.plist",
    )

    # when & then
    assert skip_reason(plain, _plist("<key>LoadPrefsFromCustomFolder</key><true/>")) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"skip_when": "Flag"},
        {"skip_because": "why"},
        {"kind": ORCA_PROFILE_STATE, "skip_when": "Flag", "skip_because": "why"},
    ],
)
def test_a_skip_rule_needs_both_halves_and_a_preferences_domain(overrides: dict[str, str]) -> None:
    # given
    fields = {
        "kind": CFPREFS_DOMAIN,
        "database": "Library/Preferences/com.example.app.plist",
        "domain": "com.example.app",
        "view": "Library/Preferences/@defaults/com.example.app.plist",
    } | overrides

    # when & then
    with pytest.raises(ConfigError):
        DocumentSource.create(**fields)


class _Store:
    """A `DocumentStore` over a fixed answer, for the skip check."""

    def __init__(self, payload: str | None, *, refuses: bool = False) -> None:
        self.payload = payload
        self.refuses = refuses

    def present(self, source: DocumentSource) -> bool:
        return self.payload is not None

    def is_symlink(self, source: DocumentSource) -> bool:
        return False

    def read(self, source: DocumentSource):
        if self.refuses:
            raise DocumentRefused("no")
        return None if self.payload is None else DocumentRead(payload=self.payload, revision="1")

    def write(self, source: DocumentSource, payload: str, *, expected_revision: str) -> None:
        raise AssertionError("the skip check never writes")


@pytest.mark.parametrize(
    ("store", "expected"),
    [
        (_Store(None), {}),
        (_Store(_plist("")), {}),
        (_Store(_plist("<key>LoadPrefsFromCustomFolder</key><true/>")), {VIEW: SOURCE.skip_because}),
        (_Store("garbage"), {VIEW: CANNOT_TELL}),
        (_Store(_plist(""), refuses=True), {VIEW: CANNOT_TELL}),
    ],
)
def test_the_skip_check_names_each_document_it_must_leave_alone(store: _Store, expected: dict[str, str]) -> None:
    # given
    check = DocumentSkipCheck({CFPREFS_DOMAIN: store}, (SOURCE,))

    # when & then
    assert dict(check.reasons()) == expected


def test_a_kind_with_no_store_is_left_alone() -> None:
    # when & then
    assert dict(DocumentSkipCheck({}, (SOURCE,)).reasons()) == {VIEW: CANNOT_TELL}


def test_a_document_with_no_skip_rule_costs_no_check_and_no_store(tmp_path) -> None:
    # when & then
    assert open_document_skips(str(tmp_path), ()) is None


def test_a_skipped_document_resolves_to_why_and_any_other_path_resolves_as_before() -> None:
    # given
    paths = LivePaths(catalog=BuiltinCatalog.default(), document_skips=((VIEW, "the app manages it"),))

    # when
    skipped, other = paths.resolve(VIEW), paths.resolve(".zshrc")

    # then
    assert (skipped, type(other).__name__) == (Unresolved("the app manages it"), "Located")
