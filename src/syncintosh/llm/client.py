# SPDX-License-Identifier: AGPL-3.0-or-later
"""An OpenAI-compatible `ConflictAdvisor`: one conflict per request, masked text only.

The advisor sends the three masked sides and the path, and reads one strict JSON answer back.
Nothing it receives is a raw value: the caller masks first (``system/masking.py``), and this module
never sees a fingerprint or a plaintext value. An answer that is not the strict shape is `None`,
so the person is asked as before (``core/advice.py``).

`openai` is imported inside the call, not at module load, so the CLI starts without it. A test
injects a `ChatTransport` and never reaches the network.
"""

from __future__ import annotations

import json
from typing import Final, Protocol

from syncintosh.core.advice import Advice, MaskedText, parse_advice

# The system prompt states the contract the answer is checked against. The model is told the
# tokens are opaque and must be copied exactly; the check in `restore` enforces it either way.
SYSTEM_PROMPT: Final = (
    "You help resolve a settings-file conflict between two machines. The texts you receive are "
    "masked: each token like <<snt:xxxxxxxx:N>> stands for a value you must not guess, so copy it "
    "exactly and use each token at most once in a proposed value. Answer with one JSON object and "
    'nothing else, with exactly the keys "choice" ("local", "remote", or "proposal"), "value" '
    '(the proposed text when choice is "proposal", otherwise null), and "rationale" (one short '
    "sentence)."
)


class ChatTransport(Protocol):
    """One chat completion: a system prompt and a user message in, the model's text out."""

    def complete(self, system: str, user: str) -> str:
        """The model's reply text. Raises when the endpoint cannot be reached."""
        ...


class OpenAIChatTransport:
    """The OpenAI-compatible chat call. Construction does no I/O; the first `complete` does.

    `base_url` is required: there is no default endpoint, so nothing reaches a hosted service the
    person did not name (``docs/design/06-llm-assist.md``). `api_key` may be `None` for a home-server
    model that needs none.
    """

    def __init__(self, *, base_url: str, model: str, api_key: str | None, timeout: float) -> None:
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        # Imported here: the package is heavy and only a `resolve --llm` run needs it.
        import httpx2
        from openai import OpenAI

        # Redirects are not followed: a redirect from the endpoint the person confirmed could send the
        # request body to a host they never confirmed.
        with httpx2.Client(timeout=self._timeout, follow_redirects=False) as http_client:
            client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key or "not-needed",
                timeout=self._timeout,
                http_client=http_client,
            )
            response = client.chat.completions.create(
                model=self._model,
                temperature=0,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        return response.choices[0].message.content or ""


class OpenAIAdvisor:
    """A `ConflictAdvisor` over any `ChatTransport`. The transport is the only thing that talks out."""

    def __init__(self, transport: ChatTransport) -> None:
        self._transport = transport

    def advise(
        self, index: int, *, path: MaskedText, base: MaskedText, local: MaskedText, remote: MaskedText
    ) -> Advice | None:
        # The index is a position in this request, not a fingerprint or a digest of any value.
        request = json.dumps(
            {"index": index, "path": path.text, "base": base.text, "local": local.text, "remote": remote.text},
            ensure_ascii=False,
        )
        return parse_advice(self._transport.complete(SYSTEM_PROMPT, request))


__all__ = ["SYSTEM_PROMPT", "ChatTransport", "OpenAIAdvisor", "OpenAIChatTransport"]
