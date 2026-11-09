# SPDX-License-Identifier: AGPL-3.0-or-later
"""The redacting config inspector must never echo a value (``AGENTS.md`` §6).

`scripts/inspect_config.py` is the sanctioned way to look at a secret-shaped file such as Docker's
`config.json`, so "it never prints a value" is the property everything else rests on. These tests
run the script and grep its stdout, which is the only thing a reader ever sees.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "inspect_config.py"

# The shapes a real `config.json` has: a credential map, an arbitrary header map, a registry list,
# and a proxy URL that carries a password.
SAMPLE = {
    "auths": {"https://index.docker.io/v1/": {"auth": "SECRET-VALUE-ONE"}},
    "HttpHeaders": {"Authorization": "SECRET-VALUE-TWO"},
    "insecure-registries": ["registry.internal.example:5000", "SECRET-VALUE-THREE"],
    "proxies": {"default": {"httpProxy": "http://user:SECRET-VALUE-FOUR@proxy.example:3128"}},
    "currentContext": "desktop-linux",
}


def _value_strings(value: object) -> list[str]:
    """Every leaf string *value*, so the check never confuses a key name with a value."""
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _value_strings(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _value_strings(item)]
    return [value] if isinstance(value, str) else []


# Computed once at module scope: a key name is structure and is printed on purpose, a value is not.
_VALUES: tuple[str, ...] = tuple(_value_strings(SAMPLE))


def _inspect(tmp_path: Path, *flags: str) -> str:
    target = tmp_path / "config.json"
    target.write_text(json.dumps(SAMPLE), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(target), *flags],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_the_inspector_never_echoes_a_value(tmp_path) -> None:
    # when — every mode the script offers
    outputs = [_inspect(tmp_path), _inspect(tmp_path, "--shape-strings"), _inspect(tmp_path, "--show-types")]

    # then — not a secret, not a registry name, not a proxy URL, not a plain setting
    leaked = [value for value in _VALUES for output in outputs if value in output]
    assert leaked == []


def test_the_inspector_keeps_the_key_structure(tmp_path) -> None:
    # when — the structure is the whole point: a preset author has to see which keys exist
    output = _inspect(tmp_path)

    # then
    assert '"auths"' in output
    assert '"HttpHeaders"' in output
    assert '"insecure-registries"' in output
    assert '"currentContext"' in output
    assert "<redacted>" in output


def test_shape_strings_labels_a_string_without_showing_it(tmp_path) -> None:
    # when
    output = _inspect(tmp_path, "--shape-strings")

    # then — a setting is distinguishable by shape, and still never printed
    assert '"currentContext": "<str:13>"' in output
    assert '"SECRET-VALUE-THREE"' not in output


# An app's settings kept as a database row (`--sqlite-document`): the same masking, a read-only open.
DOCUMENT = {
    "theme": "plain-theme-value",
    "proxyToken": "SECRET-VALUE-FIVE",
    "servers": [{"name": "internal.example", "password": "SECRET-VALUE-SIX"}, "SECRET-VALUE-SEVEN"],
    "nested": {"label": "another-plain-value"},
}


def _inspect_document(database: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite-document", str(database), "settings", *flags],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_database_document_is_printed_with_every_value_masked(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(tmp_path / "profiles" / "main" / "state.db", settings=DOCUMENT)

    # when
    outputs = [
        _inspect_document(database).stdout,
        _inspect_document(database, "--shape-strings").stdout,
        _inspect_document(database, "--show-types").stdout,
    ]

    # then — structure is shown, no value is
    leaked = [value for value in _value_strings(DOCUMENT) for output in outputs if value in output]
    assert (leaked, '"proxyToken": "<redacted>"' in outputs[0], '"nested"' in outputs[0]) == ([], True, True)


def test_a_database_document_is_opened_read_only(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(tmp_path / "profiles" / "main" / "state.db", settings=DOCUMENT)
    before = database.read_bytes()

    # when
    subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite-document", str(database), "settings"], capture_output=True, check=True
    )

    # then
    assert database.read_bytes() == before


def test_a_missing_document_or_database_fails_without_printing_a_value(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(tmp_path / "profiles" / "main" / "state.db", settings=DOCUMENT)

    # when
    missing_row = subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite-document", str(database), "nope"], capture_output=True, text=True
    )
    missing_file = subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite-document", str(tmp_path / "absent.db"), "settings"],
        capture_output=True,
        text=True,
    )

    # then
    assert (missing_row.returncode, missing_file.returncode) == (1, 1)
    assert [v for v in _value_strings(DOCUMENT) if v in missing_row.stdout + missing_row.stderr] == []


def test_a_row_that_is_not_json_fails_without_quoting_it(tmp_path, profile_state_db) -> None:
    # given
    database = profile_state_db(tmp_path / "profiles" / "main" / "state.db", settings="SECRET-VALUE-EIGHT {")

    # when
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite-document", str(database), "settings"], capture_output=True, text=True
    )

    # then
    assert (result.returncode, "SECRET-VALUE-EIGHT" in result.stdout + result.stderr) == (1, False)


@pytest.mark.parametrize("args", [[], ["some-file.json", "--sqlite-document", "a.db", "settings"]])
def test_the_inspector_wants_a_path_or_a_document_but_not_both_or_neither(args: list[str]) -> None:
    # when
    result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    # then
    assert result.returncode == 2
