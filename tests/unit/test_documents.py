# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for a database row seen as a virtual file: the source, the view codec, and the preset rules.

The payloads are TEST-ONLY stand-ins; the codec never looks at a key's name.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from syncintosh.core.documents import (
    ORCA_PROFILE_STATE,
    DocumentSource,
    compact_payload,
    content_hash,
    render_view,
    same_value,
)
from syncintosh.core.errors import ConfigError, DocumentRefused
from syncintosh.core.presets import Preset, PresetFile
from syncintosh.core.profiles import ProfileFile, ProfileSpec
from syncintosh.presets import BuiltinCatalog

DATABASE = "Library/Application Support/acme/profiles/main/state.db"
VIEW = "Library/Application Support/acme/profiles/main/@state/settings.json"
JS_PAYLOAD = '{"theme":"dark","size":13,"ratio":0.5,"nested":{"b":[1,2,{"c":null}],"a":"é"},"on":true,"big":1e21}'


def _source(**overrides: str) -> DocumentSource:
    fields = {"kind": ORCA_PROFILE_STATE, "database": DATABASE, "domain": "settings", "view": VIEW} | overrides
    return DocumentSource.create(**fields)


def _preset(*sources: DocumentSource, paths: tuple[str, ...] = (VIEW,), **kw) -> Preset:
    return Preset.create(
        name="acme",
        files=tuple(PresetFile.create(path=path, format="text") for path in paths),
        bundle_id="com.acme.app",
        documents=sources,
        **kw,
    )


# --------------------------------------------------------------------------- the source


def test_a_source_names_its_database_domain_and_view() -> None:
    # when
    source = _source()

    # then
    assert (source.database, source.domain, source.view) == (DATABASE, "settings", VIEW)
    assert source.view_directory == "Library/Application Support/acme/profiles/main/@state"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": ""},
        {"domain": ""},
        {"database": "/abs/state.db"},
        {"database": "a/../state.db"},
        {"view": "Library/acme/settings.json"},
        {"view": "Library/acme/@state/*.json"},
        {"view": "@state"},
        {"view": DATABASE, "database": DATABASE},
    ],
)
def test_a_source_that_could_name_a_real_file_is_refused(overrides: dict[str, str]) -> None:
    # when & then
    with pytest.raises(ConfigError):
        _source(**overrides)


# --------------------------------------------------------------------------- the view codec


def test_the_view_is_indented_json_with_a_final_newline_and_the_key_order_kept() -> None:
    # when
    view = render_view(JS_PAYLOAD).decode("utf-8")

    # then
    assert view.startswith('{\n  "theme": "dark",\n  "size": 13,\n  "ratio": 0.5,')
    assert view.endswith("}\n")
    assert '"a": "é"' in view


def test_a_payload_the_app_wrote_survives_view_and_back_byte_for_byte() -> None:
    # when
    restored = compact_payload(render_view(JS_PAYLOAD))

    # then — whole-valued numbers stay integers (`1e21` is where JS switches to an exponent), key order is kept
    assert restored == JS_PAYLOAD.replace("1e21", "1e+21")


@pytest.mark.parametrize(("text", "expected"), [("1.0", "1"), ("-0.0", "0"), ("100e-2", "1"), ("2.5", "2.5")])
def test_a_fraction_is_written_the_way_javascript_prints_it(text: str, expected: str) -> None:
    # when
    written = compact_payload(f'{{"n": {text}}}'.encode())

    # then
    assert written == f'{{"n":{expected}}}'


def test_compact_is_the_apps_own_form_with_no_spaces_and_no_ascii_escapes() -> None:
    # when
    written = compact_payload('{\n  "a": [1, 2],\n  "b": "é"\n}\n'.encode())

    # then
    assert written == '{"a":[1,2],"b":"é"}'


@pytest.mark.parametrize(
    "bad",
    ["", "{", "[]", "3", '"x"', "null", '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', "{" * 10000],
)
def test_a_view_that_is_not_one_json_object_is_refused_without_quoting_it(bad: str) -> None:
    # when & then
    with pytest.raises(DocumentRefused) as raised:
        compact_payload(bad.encode())
    assert "{" not in str(raised.value)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # JS writes an unpaired surrogate as an escape; it must come back as the same escape, lowercase.
        ('{"t":"\\ud83d"}', '{"t":"\\ud83d"}'),
        ('{"t":"\\ude00"}', '{"t":"\\ude00"}'),
        ('{"t":"a\\ud83db"}', '{"t":"a\\ud83db"}'),
        # A valid pair is one character, which JS writes raw.
        ('{"t":"\\ud83d\\ude00"}', '{"t":"😀"}'),
        # Low then high is two lone ones, not a pair.
        ('{"t":"\\ude00\\ud83d"}', '{"t":"\\ude00\\ud83d"}'),
        # A pair followed by a lone one.
        ('{"t":"\\ud83d\\ude00\\ude00"}', '{"t":"😀\\ude00"}'),
        # In a key the user keeps local, and in a nested value.
        ('{"k\\ud83d":1,"n":{"v":["\\ude00"]}}', '{"k\\ud83d":1,"n":{"v":["\\ude00"]}}'),
        ('{"t":"é"}', '{"t":"é"}'),
    ],
)
def test_a_lone_surrogate_round_trips_through_the_view_as_the_apps_own_escape(payload: str, expected: str) -> None:
    # when
    view = render_view(payload)
    restored = compact_payload(view)

    # then — the view is plain UTF-8, the row text is what JS writes, and the stored hash is of its UTF-8 bytes
    assert (restored, content_hash(restored), view.decode("utf-8") != "") == (
        expected,
        hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        True,
    )


@pytest.mark.parametrize("payload", ['{"t":"\\ud83d"}', '{"k\\ude00":"\\ud83d"}'])
def test_a_lone_surrogate_is_never_a_raw_unicode_error(payload: str) -> None:
    # when
    results = (render_view(payload), compact_payload(payload.encode()), same_value(payload, payload))

    # then
    assert results[2] is True


def test_what_utf8_cannot_encode_is_a_document_refusal_not_a_raw_error() -> None:
    # when & then — the hash is the one entry point that sees a decoded string from outside
    with pytest.raises(DocumentRefused):
        content_hash("\ud83d")


surrogate_fragments = st.lists(
    st.sampled_from(["a", "é", "😀", "\\ud83d", "\\ude00", "\\ud83d\\ude00", "\\\\", " "]), max_size=8
).map("".join)


@settings(deadline=None)
@given(surrogate_fragments, surrogate_fragments)
def test_property_text_with_surrogates_round_trips_and_stays_encodable(value: str, key: str) -> None:
    # given — text the way JSON spells it, so lone and paired surrogates both occur
    payload = f'{{"{key}":"{value}"}}'

    # when
    once = compact_payload(render_view(payload))
    twice = compact_payload(render_view(once))

    # then
    assert (once, content_hash(once) == hashlib.sha256(once.encode("utf-8")).hexdigest()) == (twice, True)


def test_a_view_that_is_not_utf8_is_refused() -> None:
    # when & then
    with pytest.raises(DocumentRefused):
        compact_payload(b'{"a":"\xff"}')


def test_a_payload_the_app_left_malformed_is_refused_rather_than_shown() -> None:
    # when & then
    with pytest.raises(DocumentRefused):
        render_view("not json")


@pytest.mark.parametrize(
    ("current", "proposed", "same"),
    [
        ('{"a":1,"b":[1,2]}', '{\n  "a": 1,\n  "b": [1, 2]\n}', True),
        ('{"a":1}', '{"a":1.0}', True),
        ('{"a":1}', '{"a":2}', False),
        ('{"a":1}', '{"a":1,"b":2}', False),
        ('{"a":[1,2]}', '{"a":[2,1]}', False),
        ('{"a":true}', '{"a":1}', False),
        ('{"a":1}', '{"a":true}', False),
        ('{"a":false}', '{"a":0}', False),
        ('{"a":[false]}', '{"a":[0.0]}', False),
        ('{"a":{"b":true}}', '{"a":{"b":1}}', False),
        ('{"a":true,"b":1}', '{"b":1,"a":true}', True),
    ],
)
def test_same_value_compares_json_values_not_spacing(current: str, proposed: str, same: bool) -> None:
    # when
    result = same_value(current, proposed)

    # then
    assert result is same


def test_the_content_hash_is_sha256_of_the_utf8_payload() -> None:
    # when
    digest = content_hash("é")

    # then
    assert digest == hashlib.sha256("é".encode()).hexdigest()


json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3),
    max_leaves=12,
)


@settings(deadline=None)
@given(st.dictionaries(st.text(max_size=5), json_values, max_size=5))
def test_property_view_then_compact_keeps_the_value_and_is_idempotent(document: dict) -> None:
    # given
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))

    # when
    once = compact_payload(render_view(payload))
    twice = compact_payload(render_view(once))

    # then
    assert (once, json.loads(once)) == (twice, document)


# --------------------------------------------------------------------------- the preset rules


def test_a_preset_carries_its_documents_and_the_catalog_lists_them() -> None:
    # given
    source = _source()

    # when
    catalog = BuiltinCatalog.create((_preset(source),))

    # then
    assert catalog.documents() == (source,)


@pytest.mark.parametrize(
    "build",
    [
        lambda: _preset(_source(view=VIEW.replace("settings", "other"))),
        lambda: _preset(_source(), _source()),
        lambda: _preset(_source(), paths=("Library/acme/@state/*.json",)),
        lambda: _preset(
            _source(),
            paths=(VIEW, "Library/acme/default/settings.json"),
            profiles=ProfileSpec.create(
                default_root="Library/acme/default",
                home="Library/acme/profiles",
                index="Library/acme/index.json",
                files=(ProfileFile.create(path="settings.json", resource="settings"),),
            ),
        ),
    ],
)
def test_a_document_that_is_not_an_exact_file_of_its_preset_is_refused(build) -> None:
    # when & then
    with pytest.raises(ConfigError):
        build()
