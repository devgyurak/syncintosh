# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `zsh` syntax check behind the `SyntaxValidator` port.

`zsh -fn` parses the text and stops — `-n` is `NOEXEC`, so nothing in the file is run, and `-f`
is `NO_RCS`, so the user's own startup files are not read. That is the whole point: a merged
`.zshrc` that is valid JSON-whatever but broken shell must be caught **before** it is published
or written, and running it to find out is exactly what must not happen.

The environment is minimal and the check is bounded. If `zsh` cannot be run at all the verdict
is a refusal, never a pass: a validator that fails open would let the very file it exists to
reject through (``AGENTS.md`` §3.5, "validate the suggestion before applying").

A refusal names the subject only. The text is the user's shell configuration and may hold a
token; the checker's stderr can echo a fragment of it, so neither is included
(``AGENTS.md`` §4).
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from types import MappingProxyType

from syncintosh.core.errors import SyntaxInvalid

# `zsh -f` ignores the rc files, but a hostile `ZDOTDIR` or a shell-function shim on `PATH` is
# still avoidable for free, so the check gets a fixed, minimal environment. Read-only, so a
# caller cannot mutate what every later check runs under.
_ENV: Mapping[str, str] = MappingProxyType({"PATH": "/usr/bin:/bin", "LC_ALL": "C"})

# A settings file is small; a check that hangs is a bug, not a slow file.
TIMEOUT_SECONDS = 5


class ZshSyntaxValidator:
    """A `SyntaxValidator` over `zsh -fn`."""

    def check(self, text: str, *, subject: str) -> None:
        """Raise `SyntaxInvalid` when `text` is not valid zsh; return `None` when it is."""
        try:
            result = subprocess.run(
                ["zsh", "-fn"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                env=_ENV,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SyntaxInvalid(f"{subject}: no `zsh` to validate against") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise SyntaxInvalid(f"{subject}: the zsh syntax check could not run") from exc
        if result.returncode != 0:
            raise SyntaxInvalid(f"{subject}: zsh reported a syntax error")


__all__ = ["TIMEOUT_SECONDS", "ZshSyntaxValidator"]
