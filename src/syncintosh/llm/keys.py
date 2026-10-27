# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the assisted-resolve API key comes from: the environment first, then the keyring.

The key is never in `config.json`, a fixture, or a log. A home-server model needs none, so `None`
is a normal answer, not an error.
"""

from __future__ import annotations

import os
from typing import Final

ENV_VAR: Final = "SYNCINTOSH_LLM_API_KEY"
KEYRING_SERVICE: Final = "syncintosh"
KEYRING_NAME: Final = "llm-api-key"


def llm_api_key() -> str | None:
    """The key from the environment, else from the keyring, else `None`."""
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return from_env
    # Imported here: the keyring backend is only touched by an assisted resolve.
    import keyring
    import keyring.errors

    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_NAME)
    except keyring.errors.KeyringError:
        return None


__all__ = ["ENV_VAR", "llm_api_key"]
