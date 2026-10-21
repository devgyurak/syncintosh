# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the preset catalog's shape: validation, trust, and lookup."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import ConfigError
from syncintosh.core.presets import Preset, PresetFile, download_trust
from syncintosh.presets import BuiltinCatalog, docker, vscode, zsh


def _file(path: str = "settings.json", **kwargs) -> PresetFile:
    return PresetFile.create(path=path, format="jsonc", **kwargs)


def test_a_preset_file_keeps_noise_and_excluded_apart_and_exposes_both() -> None:
    # given
    noise = {("window", "zoomLevel")}
    excluded = {("http", "proxyAuthorization")}

    # when
    file = _file(noise=noise, excluded=excluded)

    # then
    assert file.noise == frozenset(noise)
    assert file.excluded == frozenset(excluded)
    assert file.stripped == frozenset(noise | excluded)


def test_noise_on_a_text_file_is_refused() -> None:
    # when & then — a shell script has no key paths to strip, so the entry is a bug, not a no-op
    with pytest.raises(ConfigError):
        PresetFile.create(path=".zshrc", format="text", noise={("a",)})


def test_a_key_cannot_be_both_noise_and_excluded() -> None:
    # when & then — the two mean different things, and one key has one meaning
    with pytest.raises(ConfigError):
        _file(noise={("a",)}, excluded={("a",)})


def test_an_unnormalized_preset_path_is_refused() -> None:
    # when & then — the manifest would reject it later; refuse it at the catalog instead
    with pytest.raises(ConfigError):
        _file(path="../elsewhere.json")


def test_a_preset_refuses_two_entries_for_one_path() -> None:
    # given — `.Zshrc` and `.zshrc` are one file on a case-insensitive APFS
    files = (PresetFile.create(path=".zshrc", format="text"), PresetFile.create(path=".Zshrc", format="text"))

    # when & then
    with pytest.raises(ConfigError):
        Preset.create(name="zsh", files=files)


def test_a_preset_with_no_files_is_refused() -> None:
    # when & then — a preset that tracks nothing looks exactly like a successful setup
    with pytest.raises(ConfigError):
        Preset.create(name="zsh", files=())


def test_a_plain_http_download_url_is_refused() -> None:
    # when & then — only the vendor's https page is a trusted built-in (AGENTS.md §3.6)
    with pytest.raises(ConfigError):
        Preset.create(name="app", files=(_file(),), download_url="http://example.test/app")


def test_download_trust_accepts_only_the_shipped_url_verbatim() -> None:
    # given
    preset = Preset.create(name="app", files=(_file(),), download_url="https://example.test/app")

    # when
    exact = download_trust("https://example.test/app", builtin=(preset,))
    lookalike = download_trust("https://example.test/app?code.evil", builtin=(preset,))
    forged_host = download_trust("https://example.test.evil.test/app", builtin=(preset,))

    # then — the domain is always reported, so an untrusted URL can still be labelled
    assert exact.trusted is True
    assert exact.domain == "example.test"
    assert lookalike.trusted is False
    assert lookalike.domain == "example.test"
    assert forged_host.trusted is False
    assert forged_host.domain == "example.test.evil.test"


def test_the_builtin_catalog_refuses_two_presets_claiming_one_path() -> None:
    # given
    first = Preset.create(name="one", files=(_file(path="shared.json"),))
    second = Preset.create(name="two", files=(_file(path="Shared.json"),))

    # when & then
    with pytest.raises(ConfigError):
        BuiltinCatalog.create((first, second))


def test_the_catalog_matches_a_policy_by_folded_path() -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    policy = catalog.policy_for(".ZSHRC")

    # then — matched on the path's identity, never on a name that arrived from the repo
    assert policy is not None
    assert policy.path == ".zshrc"
    assert policy.validator == "zsh"


def test_the_catalog_knows_only_a_builtin_presets_own_paths() -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    owned = catalog.knows("zsh", ".zshrc")
    foreign = catalog.knows("zsh", "Library/Application Support/Code/User/settings.json")
    unknown_name = catalog.knows("my-own-thing", "anywhere/at/all")

    # then — an unknown name makes no claim, so the local allowlist alone decides
    assert owned is True
    assert foreign is False
    assert unknown_name is True


def test_every_builtin_download_url_is_https() -> None:
    # when
    plain = [
        preset.download_url
        for preset in BuiltinCatalog.default().presets
        if preset.download_url is not None and not preset.download_url.startswith("https://")
    ]

    # then — a URL is only ever a built-in preset's, so a non-https one could never be trusted
    assert plain == []


def test_the_zsh_preset_covers_the_five_startup_files() -> None:
    # when
    paths = [file.path for file in zsh.ZSH.files]

    # then
    assert paths == [".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zlogout"]
    assert {file.validator for file in zsh.ZSH.files} == {"zsh"}
    assert {file.format for file in zsh.ZSH.files} == {"text"}


def test_no_builtin_preset_claims_a_path_twice_across_the_catalog() -> None:
    # given
    catalog = BuiltinCatalog.default()

    # when
    claimed = [file.path for preset in catalog.presets for file in preset.files]

    # then
    assert sorted(claimed) == sorted(set(claimed))


def test_a_key_path_at_the_document_root_is_refused() -> None:
    # when & then — `()` would strip the whole file and make the projection a constant
    with pytest.raises(ConfigError):
        _file(excluded={()})


def test_the_vscode_preset_names_both_user_files() -> None:
    # when
    paths = [file.path for file in vscode.VSCODE.files]

    # then
    assert paths == ["Library/Application Support/Code/User/settings.json", vscode.KEYBINDINGS]


def test_the_vscode_preset_keeps_noise_and_the_proxy_credential_apart() -> None:
    # given
    settings = next(file for file in vscode.VSCODE.files if file.path == vscode.SETTINGS)

    # when
    noise = settings.noise
    excluded = settings.excluded

    # then — the dotted key VS Code writes and a nested spelling both carry the credential
    assert ("http.proxyAuthorization",) in excluded
    assert ("http", "proxyAuthorization") in excluded
    assert ("window.zoomLevel",) in noise
    assert ("git.path",) in noise
    # a machine-local key is never also a credential, and the terminal keys are synced upstream
    assert noise & excluded == frozenset()
    assert [key for key in noise if key[0].startswith("terminal.")] == []


def test_the_keybindings_file_has_no_strip_list() -> None:
    # given — an array is compared whole, so there is no key to cut
    keybindings = next(file for file in vscode.VSCODE.files if file.path == vscode.KEYBINDINGS)

    # when & then
    assert keybindings.noise == frozenset()
    assert keybindings.excluded == frozenset()
    assert keybindings.stripped == frozenset()


def test_the_vscode_preset_metadata_matches_the_verified_app() -> None:
    # when & then — the cask's zap paths give the bundle id; the vendor page gives the URL
    assert vscode.VSCODE.bundle_id == "com.microsoft.VSCode"
    assert vscode.VSCODE.cask == "visual-studio-code"
    assert vscode.VSCODE.download_url == "https://code.visualstudio.com/download"


def test_the_docker_preset_excludes_its_credential_keys() -> None:
    # given
    config = next(file for file in docker.DOCKER.files if file.path == docker.CONFIG)

    # when — the credential set and the machine-local set are read separately
    excluded = config.excluded
    noise = config.noise

    # then — a credential is cut before sealing; a helper binary's name and the active context
    # are this machine's own, so they stay here
    assert excluded == docker.CREDENTIAL_KEYS
    assert ("auths",) in excluded
    assert ("proxies",) in excluded
    assert ("HttpHeaders",) in excluded
    assert noise == docker.NOISE_KEYS
    assert ("credsStore",) in noise
    assert ("credHelpers",) in noise
    assert excluded & noise == frozenset()


def test_every_docker_file_is_checked_as_strict_json() -> None:
    # when — Docker's CLI and `dockerd` both read these with a strict JSON parser
    validators = {file.validator for file in docker.DOCKER.files}

    # then — so a render that leaves JSONC-only syntax (a trailing comma, a comment) is refused
    # rather than written (AGENTS.md §3.5)
    assert validators == {"json"}


def test_the_docker_daemon_config_excludes_its_documented_proxy_key() -> None:
    # given
    daemon = next(file for file in docker.DOCKER.files if file.path == docker.DAEMON)

    # when & then — the daemon config's `proxies` is a `user:pass@` carrier, and it was absent on
    # the inspected machine, so it is excluded on Docker's documented schema (AGENTS.md §3.4)
    assert daemon.excluded == frozenset({("proxies",)})
    assert daemon.noise == frozenset()


def test_the_docker_preset_metadata_matches_the_verified_app() -> None:
    # when & then — Info.plist, the vendor's page, and `brew info --cask` (skill Step 0-5)
    assert docker.DOCKER.bundle_id == "com.docker.docker"
    assert docker.DOCKER.cask == "docker-desktop"
    assert docker.DOCKER.download_url == "https://www.docker.com/products/docker-desktop/"
