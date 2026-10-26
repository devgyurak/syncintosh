# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the OpenAI-compatible advisor. A fake transport stands in for the network."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from syncintosh.core.advice import Advice
from syncintosh.llm.client import ChatTransport, ChatUnavailableError, OpenAIAdvisor, OpenAIChatTransport
from syncintosh.llm.keys import llm_api_key
from syncintosh.system.masking import TextMasker

HOME = "/Users/alice"
USER = "alice"
INTERNAL = "git.corp"
SECRET = "AKIAABCDEFGHIJKLMNOP"
LOCAL = f'{{"key": "{SECRET}", "host": "{INTERNAL}", "path": "{HOME}/x"}}\n'
REMOTE = f'{{"key": "other", "host": "{INTERNAL}", "path": "{HOME}/x"}}\n'


@dataclass
class FakeTransport:
    """Records every request it is given and returns a canned reply."""

    reply: str
    requests: list[str] = field(default_factory=list)

    def complete(self, system: str, user: str) -> str:
        self.requests.append(system + "\n" + user)
        return self.reply


def _advise(transport: ChatTransport) -> Advice | None:
    masker = TextMasker(home=HOME, username=USER, internal_domains=())
    path, key, base, local, remote = masker.mask(".config", "key.a", "{}", LOCAL, REMOTE)
    return OpenAIAdvisor(transport).advise(0, path=path, key=key, base=base, local=local, remote=remote)


def test_no_raw_secret_host_home_or_username_reaches_the_transport() -> None:
    # given
    transport = FakeTransport(reply='{"choice": "local", "value": null, "rationale": "keep"}')

    # when
    _advise(transport)

    # then
    sent = "\n".join(transport.requests)
    assert [needle for needle in (SECRET, INTERNAL, HOME, USER) if needle in sent] == []


def test_a_strict_answer_is_returned_as_advice() -> None:
    # given
    transport = FakeTransport(reply='{"choice": "remote", "value": null, "rationale": "theirs"}')

    # when
    advice = _advise(transport)

    # then
    assert advice == Advice(choice="remote", rationale="theirs")


def test_an_answer_outside_the_strict_shape_is_none() -> None:
    # given
    transport = FakeTransport(reply="Sure! Here is my answer: local")

    # when & then
    assert _advise(transport) is None


def test_an_unreachable_endpoint_is_no_answer_not_an_error() -> None:
    # given
    class Down:
        def complete(self, system: str, user: str) -> str:
            raise ChatUnavailableError("gone")

    # when & then — the port promises None, and the person is asked as before
    assert _advise(Down()) is None


def test_an_empty_reply_is_no_answer() -> None:
    # when & then
    assert _advise(FakeTransport(reply="")) is None


def test_the_request_names_the_key_path() -> None:
    # given
    transport = FakeTransport(reply='{"choice": "local", "value": null, "rationale": "keep"}')

    # when
    _advise(transport)

    # then — the model must know which setting it is choosing for
    assert '"key": "key.a"' in transport.requests[0]


def test_the_request_carries_an_index_and_no_fingerprint() -> None:
    # given
    transport = FakeTransport(reply='{"choice": "local", "value": null, "rationale": "keep"}')

    # when
    _advise(transport)

    # then
    assert '"index": 0' in transport.requests[0]
    assert "fingerprint" not in transport.requests[0]


def test_the_api_key_comes_from_the_environment_first(monkeypatch) -> None:
    # given — the environment is read before the keyring, so no keychain is touched here
    monkeypatch.setenv("SYNCINTOSH_LLM_API_KEY", "from-env")

    # when & then
    assert llm_api_key() == "from-env"


def test_the_chat_transport_calls_the_openai_client_with_the_given_endpoint(monkeypatch) -> None:
    # given — a stand-in `openai` module, so the lazy import is exercised without a network call
    calls: dict[str, Any] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            calls["request"] = kwargs
            message = types.SimpleNamespace(content="reply")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            calls["client"] = {key: value for key, value in kwargs.items() if key != "http_client"}
            calls["http_client"] = kwargs["http_client"]
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    class FakeOpenAIError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI, OpenAIError=FakeOpenAIError))
    transport = OpenAIChatTransport(base_url="http://127.0.0.1:8000/v1", model="m", api_key=None, timeout=5.0)

    # when
    reply = transport.complete("system text", "user text")

    # then — the endpoint is the one named, a keyless server gets a placeholder key, and nothing is sampled
    assert reply == "reply"
    assert calls["client"] == {"base_url": "http://127.0.0.1:8000/v1", "api_key": "not-needed", "timeout": 5.0}
    assert calls["http_client"].follow_redirects is False
    assert calls["request"]["temperature"] == 0
    assert calls["request"]["messages"][1] == {"role": "user", "content": "user text"}


def test_the_keyring_is_read_when_the_environment_has_no_key(monkeypatch) -> None:
    # given — the keyring call is replaced, so no real keychain is read
    import keyring

    monkeypatch.delenv("SYNCINTOSH_LLM_API_KEY", raising=False)
    monkeypatch.setattr(keyring, "get_password", lambda service, name: f"{service}:{name}")

    # when & then
    assert llm_api_key() == "syncintosh:llm-api-key"


def test_a_keyring_failure_means_no_key(monkeypatch) -> None:
    # given
    import keyring
    import keyring.errors

    def broken(service: str, name: str) -> str:
        raise keyring.errors.KeyringError("no backend")

    monkeypatch.delenv("SYNCINTOSH_LLM_API_KEY", raising=False)
    monkeypatch.setattr(keyring, "get_password", broken)

    # when & then — a home-server model needs no key, so this is not an error
    assert llm_api_key() is None


def test_the_http_client_is_httpx2_and_does_not_follow_redirects() -> None:
    # given — openai 3 takes an httpx2 client, the only one this package builds
    import httpx2

    from syncintosh.llm.client import _http_client

    # when
    client = _http_client(5.0)

    # then
    assert isinstance(client, httpx2.Client)
    assert client.follow_redirects is False
    client.close()


def test_an_openai_error_is_no_answer_and_its_text_is_not_copied(monkeypatch) -> None:
    # given — the library's message can echo the request, so it must not reach our error
    class FakeOpenAIError(Exception):
        pass

    class Failing:
        def create(self, **kwargs: object) -> object:
            raise FakeOpenAIError("echoes the key: sk-secret")

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = types.SimpleNamespace(completions=Failing())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI, OpenAIError=FakeOpenAIError))
    transport = OpenAIChatTransport(base_url="http://127.0.0.1:8000/v1", model="m", api_key="sk-secret", timeout=5.0)

    # when
    with pytest.raises(ChatUnavailableError) as raised:
        transport.complete("system", "user")

    # then
    assert "sk-secret" not in str(raised.value)
