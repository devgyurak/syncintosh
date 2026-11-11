# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LLM guardrails that live in `services/resolve.py` (``AGENTS.md`` §3.5): each rule refuses on its own."""

from __future__ import annotations

import pytest

from syncintosh.core.config import LlmConfig
from syncintosh.core.errors import LlmEndpointDeclined, LlmPolicyRefused
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.resolve import check_llm_endpoint, refuse_unseen_auto_apply

CATALOG = BuiltinCatalog.default()
PRIVATE = "http://localhost:11434/v1"
PUBLIC = "https://api.example.com/v1"


def _llm(base_url: str, *, auto_apply: tuple[str, ...] = ()) -> LlmConfig:
    return LlmConfig.create(base_url=base_url, model="m", auto_apply=auto_apply)


def _never(_prompt: str) -> bool:
    raise AssertionError("the person must not be asked")


def test_plain_http_to_a_public_host_is_refused_before_anything_is_asked() -> None:
    # given
    url = "http://api.example.com/v1"

    # when & then
    with pytest.raises(LlmPolicyRefused, match=r"llm\.base_url over plain http may not name a public host; use https"):
        check_llm_endpoint(_llm(url), url, CATALOG, _never)


def test_a_shell_preset_in_auto_apply_is_refused() -> None:
    # given
    # when & then
    with pytest.raises(LlmPolicyRefused, match=r"llm\.auto_apply names shell presets, which are always asked: zsh"):
        check_llm_endpoint(_llm(PRIVATE, auto_apply=("zsh",)), PRIVATE, CATALOG, _never)


def test_a_public_endpoint_the_person_declines_is_not_used() -> None:
    # given
    prompts: list[str] = []

    def decline(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    # when & then
    with pytest.raises(LlmEndpointDeclined):
        check_llm_endpoint(_llm(PUBLIC), PUBLIC, CATALOG, decline)
    assert prompts == [
        f"{PUBLIC} is not a loopback or private-network address: the conflicting values would leave this network. "
        "Continue?"
    ]


@pytest.mark.parametrize(("base_url", "answer"), [(PUBLIC, True), (PRIVATE, False)])
def test_a_confirmed_public_endpoint_and_a_private_one_pass(base_url, answer) -> None:
    # when & then — a private endpoint is never asked about, a public one only needs the person's yes
    check_llm_endpoint(_llm(base_url), base_url, CATALOG, lambda _prompt: answer)


def test_yes_with_auto_apply_is_refused_because_the_result_would_go_unseen() -> None:
    # given
    llm = _llm(PRIVATE, auto_apply=("zed",))

    # when & then
    with pytest.raises(LlmPolicyRefused, match="--yes would publish auto-applied proposals unseen"):
        refuse_unseen_auto_apply(llm, yes=True)


@pytest.mark.parametrize(("auto_apply", "yes"), [((), True), (("zed",), False)])
def test_yes_alone_and_auto_apply_alone_are_allowed(auto_apply, yes) -> None:
    # when & then
    refuse_unseen_auto_apply(_llm(PRIVATE, auto_apply=auto_apply), yes=yes)
