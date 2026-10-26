# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the optional `llm` section of config.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncintosh.core.config import NO_LLM, LlmConfig
from syncintosh.core.errors import ConfigError
from syncintosh.system.config import config_to_mapping, load_config


def _write(tmp_path: Path, llm: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"remote": "git@example.test:r.git", "machine_id": "mac-a", "items": [], "llm": llm}))
    return path


def test_an_llm_section_is_loaded_with_its_settings(tmp_path: Path) -> None:
    # given
    path = _write(
        tmp_path,
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "home-model",
            "internal_domains": ["corp.example"],
            "auto_apply": ["vscode"],
        },
    )

    # when
    config = load_config(path)

    # then
    assert config.llm == LlmConfig(
        base_url="http://127.0.0.1:8000/v1",
        model="home-model",
        internal_domains=("corp.example",),
        auto_apply=("vscode",),
    )


@pytest.mark.parametrize("llm", [{"base_url": "ftp://x.test"}, {"base_url": "https://"}, {"base_url": "not a url"}])
def test_a_base_url_that_is_not_an_http_url_with_a_host_is_refused(llm: dict[str, str]) -> None:
    # when & then
    with pytest.raises(ConfigError):
        LlmConfig.create(base_url=llm["base_url"])


def test_plain_http_to_a_public_host_loads_so_only_resolve_llm_refuses_it() -> None:
    # when & then — refusing at load would stop sync, watch and daemon too; `resolve --llm` checks it instead
    assert LlmConfig.create(base_url="http://api.example.com/v1").base_url == "http://api.example.com/v1"


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8000/v1", "http://nas.local:11434", "https://api.example.com/v1"],
)
def test_plain_http_to_a_private_host_and_https_to_any_host_are_accepted(base_url: str) -> None:
    # when & then
    assert LlmConfig.create(base_url=base_url).base_url == base_url


def test_an_unknown_llm_key_is_refused_so_a_typo_cannot_disable_a_guard(tmp_path: Path) -> None:
    # when & then
    with pytest.raises(ConfigError, match="unknown llm keys"):
        load_config(_write(tmp_path, {"auto_aply": ["vscode"]}))


def test_an_absent_llm_section_writes_no_llm_key() -> None:
    # given
    from syncintosh.core.config import SyncConfig

    config = SyncConfig.create(remote="r", machine_id="m", items=())

    # when & then
    assert config.llm == NO_LLM
    assert "llm" not in config_to_mapping(config)


def test_the_llm_section_holds_no_secret_field() -> None:
    # when & then — the key is read from the environment or the keyring, never stored here
    assert set(LlmConfig.__dataclass_fields__) == {"base_url", "model", "internal_domains", "auto_apply"}
