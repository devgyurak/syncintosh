# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the policy stamp: the digest, the verdict table, and what a writer stamps (issue #24)."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.core.errors import ConfigError, ManifestCorrupt
from syncintosh.core.manifest import ManifestRecord
from syncintosh.core.policy import (
    MATCH,
    NEWER,
    OLDER,
    UNKNOWN,
    PolicyStamp,
    classify_policy,
    policy_digest,
    policy_verdicts,
    stamp_policies,
)
from syncintosh.core.presets import EMPTY_CATALOG, Preset, PresetFile
from syncintosh.presets import BuiltinCatalog
from syncintosh.presets.vscode import VSCODE

A, B, C = "a" * 64, "b" * 64, "c" * 64
ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"


def _stamp(rev: int, digest: str) -> PolicyStamp:
    return PolicyStamp.create(rev=rev, digest=digest)


def _record(preset: str, file_id: str = ID1) -> ManifestRecord:
    return ManifestRecord.create(
        file_id=file_id, path=f"{preset}.conf", preset=preset, scope="common", format="text", sha256=A
    )


class _Catalog:
    """A catalog whose history is explicit, so a verdict can be driven without a real preset."""

    def __init__(self, histories: dict[str, tuple[str, ...]], presets: tuple[Preset, ...] = ()) -> None:
        self._histories = histories
        self._presets = presets

    def policy_history(self, name: str) -> tuple[str, ...] | None:
        return self._histories.get(name)

    def get(self, name: str) -> Preset | None:
        return next((preset for preset in self._presets if preset.name == name), None)

    def policy_for(self, path: str) -> PresetFile | None:
        return None

    def knows(self, preset: str, path: str) -> bool:
        return True

    def preset_for(self, path: str) -> Preset | None:
        return None

    def ignore_list_files(self) -> tuple[PresetFile, ...]:
        return ()


def _file(path: str = "a.json", **kwargs) -> PresetFile:
    return PresetFile.create(path=path, format="jsonc", **kwargs)


def _preset(*files: PresetFile, name: str = "x") -> Preset:
    return Preset.create(name=name, files=files)


@pytest.mark.parametrize(
    ("history", "stamp", "verdict"),
    [
        ((A,), _stamp(1, A), MATCH),
        ((A, B), _stamp(2, B), MATCH),
        ((A, B), _stamp(1, A), OLDER),
        ((A, B, C), _stamp(1, A), OLDER),
        ((A, B), _stamp(3, C), NEWER),
        ((A,), _stamp(2, B), NEWER),
        (None, _stamp(1, A), NEWER),
        ((), _stamp(1, A), NEWER),
        ((A, B), _stamp(1, B), UNKNOWN),
        ((A, B), _stamp(2, A), UNKNOWN),
        ((A,), _stamp(1, C), UNKNOWN),
        # No stamp is revision 1: what every repo written before stamps was made under.
        ((A,), None, MATCH),
        ((A, B), None, OLDER),
        (None, None, NEWER),
    ],
)
def test_classify_policy(history, stamp, verdict) -> None:
    # when
    result = classify_policy(history, stamp)

    # then
    assert result == verdict


@pytest.mark.parametrize(
    ("rev", "digest"),
    [(0, A), (-1, A), (True, A), (1.0, A), ("1", A), (1, "A" * 64), (1, "a" * 63), (1, ""), (1, None)],
)
def test_a_malformed_stamp_is_refused(rev, digest) -> None:
    # when & then
    with pytest.raises(ManifestCorrupt):
        PolicyStamp.create(rev=rev, digest=digest)


def test_the_digest_is_64_lowercase_hex() -> None:
    # when
    digest = policy_digest(VSCODE)

    # then
    assert len(digest) == 64
    assert digest == digest.lower()
    assert int(digest, 16) >= 0


@pytest.mark.parametrize(
    "changed",
    [
        _preset(_file("b.json")),
        _preset(PresetFile.create(path="a.json", format="toml")),
        _preset(_file(noise={("n",)})),
        _preset(_file(excluded={("e",)})),
        _preset(_file(ignore_list=True)),
        Preset.create(name="x", files=(_file(),), policy_epoch=2),
    ],
)
def test_a_policy_change_changes_the_digest(changed) -> None:
    # given
    base = _preset(_file())

    # when
    digests = (policy_digest(base), policy_digest(changed))

    # then
    assert digests[0] != digests[1]


def test_noise_and_excluded_are_different_policies() -> None:
    # given — the same key stripped for convenience or as a security invariant
    as_noise, as_excluded = _preset(_file(noise={("k",)})), _preset(_file(excluded={("k",)}))

    # when
    digests = (policy_digest(as_noise), policy_digest(as_excluded))

    # then
    assert digests[0] != digests[1]


def test_a_glob_and_the_same_text_as_an_exact_path_differ() -> None:
    # given
    glob, exact = _preset(_file("snippets/*.json")), _preset(_file("snippets/x.json"))

    # when
    digests = (policy_digest(glob), policy_digest(exact))

    # then
    assert digests[0] != digests[1]


@pytest.mark.parametrize(
    "unchanged",
    [
        _preset(_file(), name="renamed"),
        _preset(_file(validator="json")),
        _preset(_file()),
    ],
)
def test_what_does_not_decide_the_bytes_does_not_change_the_digest(unchanged) -> None:
    # given — a name, a validator: neither decides which keys an object holds
    base = _preset(_file())

    # when
    digests = (policy_digest(base), policy_digest(unchanged))

    # then
    assert digests[0] == digests[1]


def test_the_profile_layout_is_part_of_the_digest() -> None:
    # given — VS Code without its profile spec strips the same keys but lays profiles out differently
    without = Preset.create(name=VSCODE.name, files=VSCODE.files)

    # when
    digests = (policy_digest(VSCODE), policy_digest(without))

    # then
    assert digests[0] != digests[1]


@given(seed=st.integers(0, 2**16))
def test_the_digest_ignores_file_order(seed) -> None:
    # given
    files = list(VSCODE.files)
    random.Random(seed).shuffle(files)
    shuffled = Preset.create(name=VSCODE.name, files=tuple(files), bundle_id=VSCODE.bundle_id, profiles=VSCODE.profiles)

    # when
    digest = policy_digest(shuffled)

    # then
    assert digest == policy_digest(VSCODE)


@given(keys=st.sets(st.tuples(st.sampled_from("abcd"), st.sampled_from("wxyz")), min_size=1, max_size=6))
def test_the_digest_ignores_key_order(keys) -> None:
    # given
    forward, backward = sorted(keys), sorted(keys, reverse=True)

    # when
    digests = (
        policy_digest(_preset(_file(noise=forward))),
        policy_digest(_preset(_file(noise=backward))),
    )

    # then
    assert digests[0] == digests[1]


def test_verdicts_cover_stamped_and_recorded_built_in_presets() -> None:
    # given — zsh is stamped, vscode has a record but no stamp, jetbrains is a community name
    catalog = _Catalog({"zsh": (A, B), "vscode": (C,)})
    records = [_record("vscode", "1" * 32), _record("jetbrains", "2" * 32)]

    # when
    verdicts = policy_verdicts({"zsh": _stamp(2, B)}, records, catalog)

    # then
    assert verdicts == {"vscode": MATCH, "zsh": MATCH}


def test_a_stamp_for_a_preset_the_build_lacks_is_newer() -> None:
    # when
    verdicts = policy_verdicts({"future": _stamp(1, A)}, [], _Catalog({}))

    # then
    assert verdicts == {"future": NEWER}


def test_a_writer_stamps_its_own_policy_for_every_built_in_preset_with_a_record() -> None:
    # given
    preset = _preset(_file(), name="x")
    catalog = _Catalog({"x": (A, B)}, (preset,))

    # when
    stamps = stamp_policies([_record("x"), _record("community", "2" * 32)], catalog)

    # then — the revision is the history's length, the digest is computed from the preset
    assert stamps == {"x": _stamp(2, policy_digest(preset))}


def test_a_writer_stamps_nothing_without_a_history() -> None:
    # when
    stamps = stamp_policies([_record("x")], EMPTY_CATALOG)

    # then
    assert stamps == {}


def test_a_policy_changed_without_an_append_reads_back_as_unknown() -> None:
    # given — the history still ends at the old digest
    preset = _preset(_file(noise={("n",)}), name="x")
    catalog = _Catalog({"x": (A,)}, (preset,))

    # when
    stamps = stamp_policies([_record("x")], catalog)

    # then
    assert classify_policy(catalog.policy_history("x"), stamps["x"]) == UNKNOWN


def test_the_built_in_catalog_reports_a_history_only_for_its_own_presets() -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    histories = (catalog.policy_history("zsh"), catalog.policy_history("nope"))

    # then
    assert histories[0] is not None
    assert histories[1] is None


@pytest.mark.parametrize("epoch", [0, -1, True, 1.0, "1", None])
def test_a_bad_policy_epoch_is_refused(epoch) -> None:
    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="x", files=(_file(),), policy_epoch=epoch)


def test_the_default_policy_epoch_is_1() -> None:
    # when
    epoch = _preset(_file()).policy_epoch

    # then
    assert epoch == 1
