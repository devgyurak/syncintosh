# SPDX-License-Identifier: AGPL-3.0-or-later
"""The strict JSON check behind the `SyntaxValidator` port.

A file whose reader is a strict JSON parser — Docker's `config.json`, for one — cannot be handed
anything JSONC tolerates and JSON does not: a trailing comma, a comment, a bare word. The format
adapter renders in place and is allowed to preserve JSONC, so a preset that owns such a file says so
with a validator instead of hoping the render stayed strict.

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
        try:
            json.loads(text)
        except ValueError as exc:
            # `ValueError` covers `JSONDecodeError`; the message names a position, not the bytes.
            raise SyntaxInvalid(f"{subject}: not valid JSON ({exc})") from exc


__all__ = ["JsonSyntaxValidator"]
