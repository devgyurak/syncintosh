# SPDX-License-Identifier: AGPL-3.0-or-later
"""The strict JSON check behind the `SyntaxValidator` port.

A file whose reader is a strict JSON parser — Docker's `config.json`, for one — cannot be handed
anything JSONC tolerates and JSON does not: a trailing comma, a comment, a bare word. The format
adapter renders in place and is allowed to preserve JSONC, so a preset that owns such a file says so
with a validator instead of hoping the render stayed strict.

Strict means the reader's JSON, not Python's: `NaN` and `Infinity` are Python extensions Go refuses,
and a byte-order mark is stripped rather than refused, because it is not content.

The check parses the text and drops the result; nothing from the file is kept, logged, or re-raised.
A refusal names the subject and the offset only, because the file is the user's configuration and can
hold a credential (``AGENTS.md`` §4).
"""

from __future__ import annotations

import json

from syncintosh.core.errors import SyntaxInvalid


class JsonSyntaxValidator:
    """A `SyntaxValidator` over `json.loads`."""

    def check(self, text: str, *, subject: str) -> None:
        """Raise `SyntaxInvalid` when `text` is not strict JSON; return `None` when it is."""
        # A byte-order mark is a transport artifact and not content: an editor that writes one still
        # saved the user's configuration, and `json.loads` is stricter than it has to be here. Left
        # in, it would make the file **permanently** unwritable on the other machine, with a message
        # about column 1 that names nothing the user can act on.
        body = text[1:] if text.startswith("\ufeff") else text
        try:
            # `parse_constant` refuses `NaN`, `Infinity`, and `-Infinity`. Python's `json` accepts
            # them as an extension; Go's — which is what reads Docker's files — does not, so a file
            # that carries one is not a file this may write.
            json.loads(body, parse_constant=_reject_constant)
        except ValueError as exc:
            # `ValueError` covers `JSONDecodeError`; the message names a position, not the bytes.
            raise SyntaxInvalid(f"{subject}: not valid JSON ({exc})") from exc


def _reject_constant(name: str) -> object:
    """Refuse a JSON extension `json.loads` would otherwise take: `NaN`, `Infinity`, `-Infinity`."""
    raise ValueError(f"{name} is not JSON")


__all__ = ["JsonSyntaxValidator"]
