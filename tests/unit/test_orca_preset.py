# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Orca preset: an allowlist over one database row's settings (issue #55).

`tests/fixtures/orca/settings.json` holds the key names of the `settings` row as the masking inspector listed them on a
real Orca 1.4.219 (values are invented). The policy is an allowlist, so what is pinned here is the decision for every
observed key, that a credential is in no object, and that a key Orca adds later stays local.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ConfigError
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import project_text, restore_text
from syncintosh.core.watch import FileEvent
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.orca import (
    BUNDLE_ID,
    DATABASE,
    EXCLUDED,
    LOCAL,
    ORCA,
    SOURCE,
    SYNCED,
    VIEW,
)
from syncintosh.services.profile_paths import watch_targets
from syncintosh.services.setup import new_machine_config

ADAPTER = JsoncAdapter()
POLICY = ORCA.files[0]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "orca" / "settings.json"

SYNCED_NAMES = sorted(key for (key,) in SYNCED)
EXCLUDED_NAMES = sorted(key for (key,) in EXCLUDED if isinstance(key, str))
LOCAL_NAMES = sorted(name for names in LOCAL.values() for name in names)


def _live() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _text(value: dict) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _projected(value: dict) -> dict:
    return json.loads(project_text(ADAPTER, _text(value), POLICY))


def test_every_key_seen_on_the_inspected_machine_has_a_decision() -> None:
    # given
    observed = set(_live())

    # when
    undecided = sorted(observed - {*SYNCED_NAMES, *EXCLUDED_NAMES, *LOCAL_NAMES})

    # then — a key without a recorded reason would be an accident, not a decision
    assert undecided == []


def test_a_decided_key_that_was_never_seen_is_a_typo() -> None:
    # given
    observed = set(_live())

    # when — `browserKagiSessionLink` is excluded for a key an Orca build may add; every other name was observed
    unseen = sorted({*SYNCED_NAMES, *LOCAL_NAMES, *EXCLUDED_NAMES} - observed)

    # then
    assert unseen == ["browserKagiSessionLink"]


def test_no_key_is_both_carried_and_withheld() -> None:
    # given
    names = [*SYNCED_NAMES, *EXCLUDED_NAMES, *LOCAL_NAMES]

    # when
    twice = sorted({name for name in names if names.count(name) > 1})

    # then
    assert twice == []


def test_the_projection_holds_only_allowlisted_keys() -> None:
    # when
    projected = _projected(_live())

    # then
    assert sorted(set(projected) - set(SYNCED_NAMES)) == []


@pytest.mark.parametrize("key", EXCLUDED_NAMES)
def test_a_credential_key_is_in_no_object(key: str) -> None:
    # given — the key is put into the document, whatever it holds
    live = {**_live(), key: "invented-secret-placeholder"}

    # when
    projected = _projected(live)

    # then
    assert key not in projected


def test_a_key_orca_adds_later_stays_local() -> None:
    # given — neither name was ever seen, so no rule mentions it
    live = {**_live(), "brandNewPreference": True, "brandNewApiToken": "invented-secret-placeholder"}

    # when
    projected = _projected(live)

    # then
    assert {"brandNewPreference", "brandNewApiToken"} & set(projected) == set()


def test_a_custom_sound_path_is_noise_inside_a_synced_key() -> None:
    # given
    live = _live()
    live["notifications"] = {**live["notifications"], "customSoundPath": "/Users/someone/sound.wav"}

    # when
    projected = _projected(live)

    # then — the rest of the key travels, the path does not
    assert "customSoundPath" not in projected["notifications"]
    assert projected["notifications"]["enabled"] == live["notifications"]["enabled"]


def test_a_change_to_a_local_value_leaves_the_projection_as_it_was() -> None:
    # given — this machine's path, name and install id differ, and so does a dismissed prompt
    live = _live()
    other = {
        **live,
        "workspaceDir": "/Users/elsewhere/work",
        "machineName": "another-mac",
        "telemetry": {**live["telemetry"], "installId": "another-install-id"},
        "localBaseRefSuggestionDismissed": not live["localBaseRefSuggestionDismissed"],
    }

    # when & then
    assert _projected(other) == _projected(live)


def test_apply_keeps_this_machines_local_values_and_takes_the_synced_ones() -> None:
    # given — the repo object came from another Mac; this Mac has its own paths, identity and a different theme
    repo = project_text(ADAPTER, _text({**_live(), "theme": "from-the-other-mac"}), POLICY)
    live = {**_live(), "workspaceDir": "/Users/here/work", "machineName": "this-mac"}

    # when
    restored = json.loads(restore_text(ADAPTER, repo, _text(live), POLICY))

    # then
    assert (restored["workspaceDir"], restored["machineName"]) == ("/Users/here/work", "this-mac")
    assert restored["theme"] == "from-the-other-mac"
    assert restored["httpProxyUrl"] == live["httpProxyUrl"]


def test_a_restore_projects_back_to_the_object() -> None:
    # given
    repo = project_text(ADAPTER, _text(_live()), POLICY)

    # when
    restored = restore_text(ADAPTER, repo, _text({**_live(), "workspaceDir": "/Users/here/work"}), POLICY)

    # then — the next cycle reads the file as unchanged
    assert project_text(ADAPTER, restored, POLICY) == repo


@pytest.mark.parametrize("key", EXCLUDED_NAMES)
def test_moving_a_credential_key_into_the_allowlist_is_refused(key: str) -> None:
    # when & then
    with pytest.raises(ConfigError):
        PresetFile.create(path=VIEW, format="jsonc", synced={*SYNCED, (key,)}, excluded=EXCLUDED)


def test_the_catalog_owns_the_virtual_path_and_nothing_beside_it() -> None:
    # given
    catalog = BuiltinCatalog.default()
    database = DATABASE
    export = DATABASE.replace("profile-state.db", "orca-data.json")

    # when
    owners = [catalog.preset_for(path) for path in (VIEW, database, export)]

    # then — the database and the stale export are not files this preset owns
    assert [owner.name if owner else None for owner in owners] == ["orca", None, None]
    assert catalog.policy_for(VIEW) is POLICY
    assert catalog.policy_for(database) is None


@pytest.mark.parametrize("path", [DATABASE, DATABASE.replace("profile-state.db", "orca-data.json")])
def test_a_record_may_not_name_the_database_or_the_export(path: str) -> None:
    # when & then
    assert BuiltinCatalog.default().knows("orca", path) is False


def test_the_catalog_lists_the_document_the_row_is_read_from() -> None:
    # when
    sources = BuiltinCatalog.default().documents()

    # then
    assert SOURCE in sources


def test_the_preset_metadata_is_what_step_zero_verified() -> None:
    # when & then — no cask: Homebrew's `orca` is another vendor's app
    assert (ORCA.bundle_id, ORCA.cask, ORCA.defer_while_running) == (BUNDLE_ID, None, True)
    assert ORCA.download_url == "https://github.com/stablyai/orca/releases"
    assert BUNDLE_ID == "com.stablyai.orca"


def test_the_policy_is_on_the_exact_profile_the_decision_names() -> None:
    # when & then — `local-default` only (D9)
    assert VIEW == "Library/Application Support/orca/profiles/local-default/@profile-state/settings.json"
    assert SOURCE.database == DATABASE
    assert SOURCE.domain == "settings"


def test_a_watch_leaves_the_virtual_path_out() -> None:
    # given — the database is rewritten by Orca every few seconds for state this tool never carries
    catalog = BuiltinCatalog.default()
    config = new_machine_config(remote="r", machine_id="m", preset_names=("orca", "zsh"), catalog=catalog)

    # when
    targets = watch_targets(config, catalog, None)

    # then — the row is collected by the periodic sync; an event under its folder wakes nothing, and zsh still does
    assert VIEW in [item.path for item in config.items]
    assert targets.matches(FileEvent(VIEW)) is False
    assert targets.directories() == ("",)
