# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the preset catalog's shape: validation, trust, and lookup."""

from __future__ import annotations

import pytest

from syncintosh.core.errors import ConfigError
from syncintosh.core.presets import (
    EMPTY_CATALOG,
    Preset,
    PresetFile,
    PresetGlob,
    ProposedUrl,
    ShippedPresets,
    download_trust,
    proposed_urls,
)
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

    shipped = ShippedPresets.create((preset,))

    # when
    exact = download_trust("https://example.test/app", shipped=shipped)
    lookalike = download_trust("https://example.test/app?code.evil", shipped=shipped)
    forged_host = download_trust("https://example.test.evil.test/app", shipped=shipped)

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


def test_the_vscode_preset_names_its_user_files_and_snippet_globs() -> None:
    # when
    paths = [file.path for file in vscode.VSCODE.files]

    # then — the two exact files, then the two snippet globs (a directory of per-machine files)
    assert paths == [
        vscode.SETTINGS,
        vscode.KEYBINDINGS,
        vscode.SNIPPETS_JSON,
        vscode.SNIPPETS_GLOBAL,
    ]


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


# --------------------------------------------------------------------------- preset_for


def test_preset_for_returns_the_owning_preset_matched_on_path() -> None:
    # given — the lookup is by path, never by the preset name a record carries
    catalog = BuiltinCatalog.default()

    # when
    vscode_preset = catalog.preset_for(vscode.SETTINGS)
    docker_preset = catalog.preset_for(docker.CONFIG)
    zsh_preset = catalog.preset_for(zsh.STARTUP_FILES[0])

    # then
    assert vscode_preset is not None
    assert vscode_preset.name == "vscode"
    assert docker_preset is not None
    assert docker_preset.name == "docker"
    assert zsh_preset is not None
    assert zsh_preset.name == "zsh"
    assert catalog.preset_for("Documents/notes.md") is None


def test_preset_for_folds_case_and_normalization() -> None:
    # given — APFS is case-insensitive, so a differently-cased path names the same file
    catalog = BuiltinCatalog.default()

    # when
    found = catalog.preset_for(vscode.SETTINGS.upper())

    # then
    assert found is not None
    assert found.name == "vscode"


def test_the_empty_catalog_owns_no_path() -> None:
    # when & then
    assert EMPTY_CATALOG.preset_for(vscode.SETTINGS) is None


# --------------------------------------------------------------------------- globs


def test_a_glob_only_sits_in_the_last_element() -> None:
    # given — a pattern in a directory element would name a whole tree, not one directory
    # when & then
    with pytest.raises(ConfigError):
        PresetGlob.create("Library/*/snippets/file.json")


def test_a_recursive_glob_is_refused() -> None:
    # given — `**` would turn the expansion from one listing into an unbounded walk
    # when & then
    with pytest.raises(ConfigError):
        PresetGlob.create("Library/Application Support/Code/User/snippets/**")


def test_a_glob_that_names_no_pattern_is_refused() -> None:
    # given — an exact path belongs in a `PresetFile`, not a `PresetGlob`
    # when & then
    with pytest.raises(ConfigError):
        PresetGlob.create("Library/Application Support/Code/User/settings.json")


def test_a_glob_matches_its_directory_and_last_element() -> None:
    # given
    glob = PresetGlob.create("Library/Application Support/Code/User/snippets/*.json")

    # when & then — the comparison is folded, so a differently-cased path names the same file
    assert glob.matches("Library/Application Support/Code/User/snippets/javascript.json") is True
    assert glob.matches("library/application support/code/user/snippets/JAVASCRIPT.json") is True
    assert glob.matches("Library/Application Support/Code/User/snippets/global.code-snippets") is False
    assert glob.matches("Library/Application Support/Code/User/settings.json") is False


def test_a_question_mark_matches_one_character() -> None:
    # given
    glob = PresetGlob.create("dir/v?.json")

    # when & then
    assert glob.expand(["v1.json", "v12.json", "v.json"]) == ("dir/v1.json",)


def test_expand_prefixes_the_directory_and_sorts() -> None:
    # given
    glob = PresetGlob.create("a/b/*.json")

    # when
    expanded = glob.expand(["z.json", "a.json", "note.txt"])

    # then
    assert expanded == ("a/b/a.json", "a/b/z.json")


def test_expand_of_a_home_root_glob_has_no_prefix() -> None:
    # given — a glob in `$HOME` itself, so the directory element is empty
    glob = PresetGlob.create("*.json")

    # when & then
    assert glob.expand(["a.json"]) == ("a.json",)


def test_a_preset_file_knows_whether_it_is_a_glob() -> None:
    # given
    exact = PresetFile.create(path="a/b.json", format="jsonc")
    globbed = PresetFile.create(path="a/*.json", format="jsonc")

    # when & then
    assert exact.is_glob is False
    assert globbed.is_glob is True
    assert globbed.matches("a/x.json") is True
    assert exact.matches("a/b.json") is True
    assert exact.matches("a/c.json") is False


def test_a_glob_over_another_presets_exact_path_is_refused() -> None:
    # given — a glob that owns a directory a built-in preset already owns one file of
    overlapping = Preset.create(
        name="overlap",
        files=(PresetFile.create(path="Library/Application Support/Code/User/*.json", format="jsonc"),),
    )

    # when & then
    with pytest.raises(ConfigError):
        BuiltinCatalog.create((*BuiltinCatalog.default().presets, overlapping))


def test_the_catalog_matches_a_path_a_glob_owns() -> None:
    # given
    catalog = BuiltinCatalog.default()
    snippet = vscode.SNIPPETS_JSON.replace("*", "javascript")

    # when
    policy = catalog.policy_for(snippet)
    preset = catalog.preset_for(snippet)

    # then — the glob's policy is the snippet file's, and the owning preset is reported so a
    # deferral for a snippet carries its download page and cask too
    assert policy is not None
    assert policy.is_glob is True
    assert preset is not None
    assert preset.name == "vscode"
    assert catalog.knows("vscode", snippet) is True
    assert catalog.knows("zsh", snippet) is False


def test_a_glob_in_a_directory_element_is_refused() -> None:
    # given — a metacharacter outside the last element would name a literal directory
    # when & then
    with pytest.raises(ConfigError):
        PresetGlob.create("a/**/b/*.json")


def test_a_preset_file_with_a_metacharacter_outside_the_last_element_is_refused() -> None:
    # given — neither an exact path nor a last-element glob
    # when & then
    with pytest.raises(ConfigError):
        PresetFile.create(path="a/*/b.json", format="jsonc")


def test_a_glob_does_not_match_its_own_pattern_text() -> None:
    # given — a record that arrived from the repo carrying the pattern as its path
    glob = PresetGlob.create("d/*.json")

    # when & then — the pattern text is not a file the glob names, so `apply` cannot create one
    assert glob.matches("d/*.json") is False
    assert glob.matches("d/?x.json") is False
    assert glob.matches("d/x.json") is True


def test_a_glob_over_its_own_presets_exact_path_is_refused() -> None:
    # given — one preset declaring both a directory glob and a file inside it
    ambiguous = Preset.create(
        name="ambiguous",
        files=(
            PresetFile.create(path="d/*.json", format="jsonc"),
            PresetFile.create(path="d/secret.json", format="jsonc", excluded={("token",)}),
        ),
    )

    # when & then — which policy applied would depend on declaration order, so it is refused
    with pytest.raises(ConfigError):
        BuiltinCatalog.create((ambiguous,))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test.evil.example/app",
        "https://evil.example/?https://example.test/app",
        "https://example.test@evil.example/app",
        "https://example.test/app/",
        "http://example.test/app",
    ],
)
def test_a_lookalike_of_a_shipped_url_is_never_trusted(url: str) -> None:
    # given
    preset = Preset.create(name="app", files=(_file(),), download_url="https://example.test/app")
    shipped = ShippedPresets.create((preset,))

    # when
    trust = download_trust(url, shipped=shipped)

    # then
    assert trust.trusted is False


def test_a_proposed_url_is_untrusted_even_when_it_matches_a_shipped_one() -> None:
    # given — the proposal's text is the only place the URL came from

    # when
    found = proposed_urls("open https://example.test/app", base="", local="", remote="")

    # then — new to every side, so it is flagged, and it is never trusted
    assert found == (ProposedUrl(url="https://example.test/app", domain="example.test"),)
    assert found[0].trusted is False


def test_a_url_a_side_already_held_is_not_flagged() -> None:
    # when
    found = proposed_urls("see https://example.test/app", base="", local="https://example.test/app", remote="")

    # then
    assert found == ()


def test_proposed_urls_drop_trailing_punctuation_and_name_the_real_host() -> None:
    # when
    found = proposed_urls(
        "get https://evil.example/x. or https://example.test@evil.example/y", base="", local="", remote=""
    )

    # then — the host is the one a browser would reach, not the userinfo that looks like a vendor
    assert [(item.url, item.domain) for item in found] == [
        ("https://evil.example/x", "evil.example"),
        ("https://example.test@evil.example/y", "evil.example"),
    ]


def test_a_proposal_with_non_ascii_url_characters_is_read_without_error() -> None:
    # when — a full-width slash and a full-width host would make `urlsplit` raise if the pattern admitted them
    found = proposed_urls("see https://evil.example／x and https://ｅｖｉｌ.example/y", base="", local="", remote="")

    # then — the ASCII part is found and nothing raises
    assert [item.url for item in found] == ["https://evil.example"]
    assert all(item.trusted is False for item in found)


@pytest.mark.parametrize(
    "proposal",
    [
        "open HTTPS://evil.example/x",
        r'{"a": "https:\/\/evil.example\/x"}',
        r'{"a": "https://evil.example/x"}',
    ],
    ids=["upper-scheme", "json-escaped-slash", "json-unicode-escape"],
)
def test_a_url_written_in_any_escaped_form_is_still_found(proposal: str) -> None:
    # when
    found = proposed_urls(proposal, base="", local="", remote="")

    # then — the domain the person sees is the real one, and the URL is untrusted
    assert [item.domain for item in found] == ["evil.example"]
    assert all(item.trusted is False for item in found)


def test_a_url_in_a_side_is_not_new_even_when_the_proposal_escapes_it() -> None:
    # when
    found = proposed_urls(r'{"a": "https:\/\/example.test\/app"}', base="", local="https://example.test/app", remote="")

    # then
    assert found == ()
