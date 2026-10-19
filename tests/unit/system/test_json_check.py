# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the strict JSON check behind the `SyntaxValidator` port."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import SyntaxInvalid
from syncintosh.system.json_check import JsonSyntaxValidator

VALID = '{\n  "psFormat": "b",\n  "imagesFormat": "table"\n}\n'


def test_a_strict_json_document_passes() -> None:
    # when & then
    JsonSyntaxValidator().check(VALID, subject=".docker/config.json")


@pytest.mark.parametrize(
    "text",
    [
        '{\n  "psFormat": "b",\n}',  # a trailing comma: JSONC accepts it, JSON does not
        '{\n  // note\n  "psFormat": "b"\n}',  # and a comment
        '{\n  "psFormat": "b"\n',  # a document that does not close
    ],
)
def test_jsonc_that_strict_json_would_refuse_is_refused(text: str) -> None:
    # when & then — this is the point of the validator: a strict reader must not be handed it
    with pytest.raises(SyntaxInvalid):
        JsonSyntaxValidator().check(text, subject=".docker/config.json")


def test_a_refusal_names_the_subject_and_never_the_value() -> None:
    # given
    text = '{\n  "proxyAuthorization": "Basic MARKER-abc123",\n}\n'

    # when
    with pytest.raises(SyntaxInvalid) as refusal:
        JsonSyntaxValidator().check(text, subject=".docker/config.json")

    # then — the file is the user's configuration and may hold a credential (AGENTS.md §4)
    assert ".docker/config.json" in str(refusal.value)
    assert "MARKER-abc123" not in str(refusal.value)
