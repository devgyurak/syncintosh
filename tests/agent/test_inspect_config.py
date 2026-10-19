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
