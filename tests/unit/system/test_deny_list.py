# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the dangerous-path deny-list.

The bypass that matters is a symlink: a preset naming an innocent path whose target is
``~/.ssh`` must still be refused (``AGENTS.md`` §3.4).
"""

from __future__ import annotations

import pytest

from syncintosh.system.deny_list import DENIED_PATHS, SystemDenyList, is_denied


@pytest.mark.parametrize("denied", DENIED_PATHS)
def test_each_denied_path_is_refused(tmp_path, denied: str) -> None:
    # given
    home = tmp_path / "home"
    target = home / denied

    # when & then
    assert is_denied(target, home) is True


@pytest.mark.parametrize(
    "relative",
    [".zshrc", "Documents/notes.md", "Library/Application Support/Code/User/settings.json"],
)
def test_an_ordinary_path_is_allowed(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / relative, home) is False


@pytest.mark.parametrize("variant", [".SSH/id_ed25519", "library/COOKIES/x", "Library/Caches/y", ".Kube/config"])
def test_a_case_variant_of_a_denied_path_is_refused(tmp_path, variant: str) -> None:
    # given — APFS is case-insensitive, so these name the very same files
    home = tmp_path / "user"

    # when & then — a case-sensitive comparison here would be a bypass
    assert is_denied(home / variant, home) is True


def test_a_symlink_into_a_denied_tree_is_refused(tmp_path) -> None:
    # given — `~/notes/link` resolves into `~/.ssh`, while its own path looks ordinary
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / "notes").mkdir()
    (home / "notes" / "link").symlink_to(home / ".ssh" / "id_rsa")

    # when & then — judged by the real path, the link target is denied
    assert is_denied(home / "notes" / "link", home) is True


def test_a_denied_directory_replaced_by_a_symlink_is_still_refused(tmp_path) -> None:
    # given — `~/Library/Cookies` is a symlink pointing elsewhere
    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    (home / "elsewhere").mkdir()
    (home / "Library" / "Cookies").symlink_to(home / "elsewhere")

    # when & then — the path as written still names a denied tree
    assert is_denied(home / "Library" / "Cookies" / "cookies.txt", home) is True


def test_a_path_outside_home_is_not_denied_by_this_check(tmp_path) -> None:
    # given — outside-home escapes are `apply`'s trust-boundary check, not the deny-list's
    home = tmp_path / "home"

    # when & then
    assert is_denied(tmp_path / "outside" / ".ssh" / "id_rsa", home) is False


def test_the_port_adapter_judges_a_home_relative_path(tmp_path) -> None:
    # given
    home = tmp_path / "home"
    adapter = SystemDenyList()

    # when & then
    assert adapter.is_denied(str(home), ".ssh/id_rsa") is True
    assert adapter.is_denied(str(home), ".zshrc") is False


def test_a_path_under_home_does_not_escape(tmp_path) -> None:
    # given
    home = tmp_path / "user"
    home.mkdir(parents=True)

    # when & then
    assert SystemDenyList().escapes(str(home), "Library/Preferences/x.plist") is False


def test_a_symlinked_parent_that_points_outside_home_escapes(tmp_path) -> None:
    # given — `~/redirect` is a symlink to a directory outside the home tree
    home = tmp_path / "user"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "redirect").symlink_to(outside)

    # when & then
    assert SystemDenyList().escapes(str(home), "redirect/evil.plist") is True


def test_a_relative_path_with_a_dotdot_component_escapes(tmp_path) -> None:
    # given
    home = tmp_path / "user"
    home.mkdir()

    # when & then — the manifest validator also refuses these, but the guard must not rely on it
    assert SystemDenyList().escapes(str(home), "../evil") is True


def test_docker_context_tls_material_is_denied_as_a_subtree(tmp_path) -> None:
    # given
    home = tmp_path / "home"

    # when & then — a private key anywhere under the directory is refused
    assert is_denied(home / ".docker" / "contexts" / "tls" / "key.pem", home) is True
    assert is_denied(home / ".docker" / "contexts" / "tls" / "registry" / "ca.pem", home) is True


def test_a_case_variant_of_the_docker_tls_directory_is_denied(tmp_path) -> None:
    # given — APFS is case-insensitive, so this names the same tree
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / ".DOCKER" / "Contexts" / "TLS" / "key.pem", home) is True


def test_the_docker_config_beside_the_tls_tree_is_still_allowed(tmp_path) -> None:
    # given — the preset tracks this file; only the TLS subtree is dangerous
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / ".docker" / "config.json", home) is False
    assert is_denied(home / ".docker" / "contexts" / "meta" / "meta.json", home) is False


def test_the_machine_local_state_directory_is_denied(tmp_path) -> None:
    # given — the ledger, the pending plaintext, and the scan allowlist (with its HMAC key) live
    # under `~/.local/share/syncintosh`; a user-added item must not be able to collect them
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / ".local" / "share" / "syncintosh" / "scan-allow.json", home) is True
    assert is_denied(home / ".LOCAL" / "share" / "syncintosh" / "applied.json", home) is True


def test_the_state_directory_is_denied_even_under_a_moved_data_root(tmp_path, monkeypatch) -> None:
    # given — `XDG_DATA_HOME` moves the ledger, the pending plaintext, and the allowlist key away
    # from the default, so the literal home-relative entry alone would not cover them
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "elsewhere"))

    # when & then
    assert SystemDenyList().is_denied(str(home), "elsewhere/syncintosh/scan-allow.json") is True


# --------------------------------------------------------------------------- JetBrains release folders (wildcards)

JB = "Library/Application Support/JetBrains"


@pytest.mark.parametrize(
    "relative",
    [
        f"{JB}/DataGrip2026.2/ssl/trust.jks",
        f"{JB}/DataGrip2026.2/jdbc-drivers/driver.jar",
        f"{JB}/IntelliJIdea2026.3/consoles/db/console.sql",
        f"{JB}/DataGrip2026.2/dataSourcesHistory/x",
        f"{JB}/DataGrip2026.2/workspace/x.xml",
        f"{JB}/DataGrip2026.2/tasks/x",
        f"{JB}/DataGrip2026.2/settingsSync/x",
        f"{JB}/DataGrip2026.2/options/dataSources/uuid.xml",
        f"{JB}/DataGrip2026.2/options/dataSources.xml",
        f"{JB}/DataGrip2026.2/options/dataSources.local.xml",
        f"{JB}/DataGrip2026.2/options/security.xml",
        f"{JB}/DataGrip2026.2/c.kdbx",
        f"{JB}/DataGrip2026.2/c.pwd",
        f"{JB}/DataGrip2026.2/id_rsa.key",
        f"{JB}/@DataGrip/options/dataSources.xml",
        f"{JB}/@DataGrip/ssl/x",
    ],
)
def test_a_jetbrains_credential_path_is_denied_whichever_release_folder_it_is_in(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / relative, home) is True


@pytest.mark.parametrize(
    "relative",
    [
        f"{JB}/DataGrip2026.2/options/editor-font.xml",
        f"{JB}/DataGrip2026.2/options/ide.general.xml",
        f"{JB}/DataGrip2026.2/colors/Dark.icls",
        f"{JB}/Toolbox/apps/DataGrip/state.json",
        f"{JB}/Toolbox/ssl_unrelated",
        f"{JB}/DataGrip2026.2/options/sub/security.xml",
        f"{JB}/DataGrip2026.2/sub/c.pwd",
        f"{JB}/DataGrip2026.2/sub/id.key",
        f"{JB}/DataGrip2026.2",
        JB,
        "Library/Application Support/JetBrainsOther/DataGrip2026.2/ssl/x",
    ],
)
def test_a_jetbrains_path_that_holds_no_credential_is_allowed(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then — a wildcard is one element and never recursive, and the sibling `Toolbox/` is not a release folder
    assert is_denied(home / relative, home) is False


@pytest.mark.parametrize(
    "variant",
    [
        f"{JB.lower()}/datagrip2026.2/SSL/x",
        f"{JB.upper()}/DATAGRIP2026.2/OPTIONS/DATASOURCES.XML",
        f"{JB}/DataGrip2026.2/ID.KEY",
    ],
)
def test_a_case_variant_of_a_jetbrains_credential_path_is_denied(tmp_path, variant: str) -> None:
    # given — APFS is case-insensitive
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / variant, home) is True


def test_a_decomposed_spelling_of_a_jetbrains_path_is_denied(tmp_path) -> None:
    # given — the release folder is spelled with a decomposed character; the denied name is not touched, so the
    # wildcard element is what normalization must not break
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / JB / "Café" / "ssl" / "x", home) is True


def test_a_symlink_into_a_jetbrains_credential_directory_is_denied(tmp_path) -> None:
    # given — an innocent-looking file whose target is a release folder's `ssl` directory
    home = tmp_path / "home"
    (home / JB / "DataGrip2026.2" / "ssl").mkdir(parents=True)
    (home / "notes").mkdir()
    (home / "notes" / "link").symlink_to(home / JB / "DataGrip2026.2" / "ssl" / "trust.jks")

    # when & then
    assert is_denied(home / "notes" / "link", home) is True


def test_a_release_credential_directory_that_is_a_symlink_out_of_the_tree_is_still_denied(tmp_path) -> None:
    # given — `ssl` is a symlink pointing elsewhere; the name as written is what names the credential
    home = tmp_path / "home"
    (home / JB / "DataGrip2026.2").mkdir(parents=True)
    (home / "elsewhere").mkdir()
    (home / JB / "DataGrip2026.2" / "ssl").symlink_to(home / "elsewhere")

    # when & then
    assert is_denied(home / JB / "DataGrip2026.2" / "ssl" / "x", home) is True


def test_the_jetbrains_folder_itself_being_a_symlink_does_not_hide_a_credential(tmp_path) -> None:
    # given — the whole vendor folder lives on another volume
    home = tmp_path / "home"
    (home / "Library" / "Application Support").mkdir(parents=True)
    moved = tmp_path / "moved"
    (moved / "DataGrip2026.2" / "ssl").mkdir(parents=True)
    (home / JB).symlink_to(moved)

    # when & then
    assert is_denied(home / JB / "DataGrip2026.2" / "ssl" / "x", home) is True


# ---------------------------------------------------------------- JetBrains options/ files that hold hosts and tokens

SENSITIVE_OPTIONS = [
    "dataSources.xml",
    "dataSources.local.xml",
    "dataSourcesExtra.xml",
    "security.xml",
    "remote-servers.xml",
    "sshConfigs.xml",
    "webServers.xml",
    "webDeployTransportHostTargets.xml",
    "databaseDrivers.xml",
    "certificates.xml",
    "proxy.settings.xml",
    "github.xml",
    "gitlab.xml",
    "docker.xml",
    "docker-registry.xml",
    "kubernetes-settings.xml",
    "http-client.xml",
    "sshRecentConnections.xml",
    "sshRecentConnections-2.xml",
    "wslRecentConnections.xml",
    "wslRecentConnections-2.xml",
    "recentConnections.xml",
    "trusted-paths.xml",
]


@pytest.mark.parametrize("name", SENSITIVE_OPTIONS)
@pytest.mark.parametrize("folder", ["RustRover2026.3", "IntelliJIdea2026.2", "@PhpStorm"])
def test_a_jetbrains_options_file_with_a_host_or_a_token_is_denied_in_any_product_folder(
    tmp_path, folder: str, name: str
) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / JB / folder / "options" / name, home) is True


@pytest.mark.parametrize("name", SENSITIVE_OPTIONS)
def test_the_case_and_normalization_of_a_sensitive_options_file_do_not_matter(tmp_path, name: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / JB.upper() / "RUSTROVER2026.3" / "OPTIONS" / name.upper(), home) is True


@pytest.mark.parametrize(
    "relative",
    [
        f"{JB}/RustRover2026.3/options/editor-font.xml",
        f"{JB}/RustRover2026.3/options/mac/keymap.xml",
        f"{JB}/RustRover2026.3/options/dockerfile.xml",
        f"{JB}/RustRover2026.3/options/githubish.xml",
        f"{JB}/RustRover2026.3/keymaps/Mine.xml",
        f"{JB}/Toolbox/apps/RustRover/github.xml",
        f"{JB}/Toolbox/apps/RustRover/security.xml",
        f"{JB}/RustRover2026.3/options/sub/github.xml",
    ],
)
def test_a_neighbouring_settings_file_is_not_a_false_positive(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / relative, home) is False


def test_a_symlink_to_a_sensitive_options_file_is_denied(tmp_path) -> None:
    # given
    home = tmp_path / "home"
    target = home / JB / "RustRover2026.3" / "options" / "sshConfigs.xml"
    target.parent.mkdir(parents=True)
    target.write_text("<application />", encoding="utf-8")
    (home / "notes").mkdir()
    (home / "notes" / "link").symlink_to(target)

    # when & then
    assert is_denied(home / "notes" / "link", home) is True


@pytest.mark.parametrize("name", SENSITIVE_OPTIONS)
def test_a_sensitive_options_file_is_also_denied_under_the_per_os_folder(tmp_path, name: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / JB / "RustRover2026.3" / "options" / "mac" / name, home) is True


# ------------------------------------------------------------------------------------------ Orca's data folder

ORCA_ROOT = "Library/Application Support/orca"
ORCA_VIEW = f"{ORCA_ROOT}/profiles/local-default/@profile-state/settings.json"


@pytest.mark.parametrize(
    "name",
    [
        "orca-secret-protection.json",
        "orca-e2ee-keypair.json",
        "agent-session-authority.key",
        "Cookies",
        "Cookies-journal",
        "codex-pane-accounts.json",
        "orca-devices.json",
        "browser-host-client-identity.json",
        "orca-github-cache.json",
        "profile-state.db",
        "profile-state.db-wal",
        "profile-state.db-shm",
        "orca-data.json",
        "orca-data.json.bak.0",
        "orca-data.json.sqlite-export.5011.json",
    ],
)
@pytest.mark.parametrize(
    "folder", [ORCA_ROOT, f"{ORCA_ROOT}/profiles/local-default", f"{ORCA_ROOT}/profiles/another-id"]
)
def test_an_orca_secret_or_state_file_is_denied_in_the_data_root_and_in_every_profile(
    tmp_path, folder: str, name: str
) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / folder / name, home) is True


@pytest.mark.parametrize(
    "relative",
    [
        "daemon/daemon-v38.sock",
        "daemon/daemon-v38.token",
        "daemon/daemon-v38.pid",
        "Network/Cookies",
        "Network/Trust Tokens",
        "Local Storage/leveldb/000003.log",
        "Session Storage/000003.log",
        "IndexedDB/file__0.indexeddb.leveldb/CURRENT",
    ],
)
@pytest.mark.parametrize(
    "folder", [ORCA_ROOT, f"{ORCA_ROOT}/profiles/local-default", f"{ORCA_ROOT}/profiles/another-id"]
)
def test_everything_in_an_orca_daemon_or_electron_store_folder_is_denied_in_the_root_and_every_profile(
    tmp_path, folder: str, relative: str
) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / folder / relative, home) is True


def test_the_virtual_settings_path_is_not_denied(tmp_path) -> None:
    # given — the one path the preset syncs; no file is called `@profile-state`
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / ORCA_VIEW, home) is False


@pytest.mark.parametrize(
    "relative",
    [
        "Partitions/orca-browser/Cookies",
        "Partitions/orca-browser/Cookies-journal",
        "Partitions/orca-browser/Local Storage/leveldb/000003.log",
        "Partitions/orca-browser/IndexedDB/https_example.com_0.indexeddb.leveldb/CURRENT",
        "codex-runtime-home/system-default-auth.json",
        "codex-runtime-home/shared-runtime-auth-provenance.json",
        "terminal-history/session-1.log",
        "orchestration.db",
        "orchestration.db-wal",
        "a-folder-orca-adds-next-year/anything.json",
        "orca-profile-index.json",
        "Preferences",
        "profiles/local-default/orca-data.json",
        "profiles/local-default/@profile-state/other.json",
        "profiles/local-default/@profile-state",
        "profiles/another-id/@profile-state/settings.json",
    ],
)
def test_the_whole_orca_data_tree_is_denied_so_a_folder_orca_adds_later_is_too(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / ORCA_ROOT / relative, home) is True


@pytest.mark.parametrize(
    "relative",
    [
        "Library/Application Support/orca-notes/settings.json",
        "Library/Application Support/Code/User/settings.json",
        "Library/Application Support/orcas/profiles/local-default/@profile-state/settings.json",
    ],
)
def test_a_neighbouring_folder_is_not_a_false_positive(tmp_path, relative: str) -> None:
    # given
    home = tmp_path / "home"

    # when & then
    assert is_denied(home / relative, home) is False


def test_the_virtual_view_is_exempt_only_under_the_real_home_spelling(tmp_path) -> None:
    # given — the data folder is a symlink, so the view's real path is somewhere else
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "profiles" / "local-default").mkdir(parents=True)
    (home / "Library" / "Application Support").mkdir(parents=True)
    (home / ORCA_ROOT).symlink_to(elsewhere)

    # when & then — the written path is the view, the real path is not, so the exemption does not apply
    assert is_denied(home / ORCA_VIEW, home) is True


def test_a_real_file_behind_the_views_name_is_still_denied(tmp_path) -> None:
    # given — someone made a real `@profile-state` folder whose settings.json is a link to the database
    home = tmp_path / "home"
    profile = home / ORCA_ROOT / "profiles" / "local-default"
    (profile / "@profile-state").mkdir(parents=True)
    database = profile / "profile-state.db"
    database.write_bytes(b"")
    (profile / "@profile-state" / "settings.json").symlink_to(database)

    # when & then — it resolves into the denied tree, so the exemption does not apply
    assert is_denied(home / ORCA_VIEW, home) is True


def test_a_symlink_to_orcas_state_database_is_denied(tmp_path) -> None:
    # given
    home = tmp_path / "home"
    target = home / ORCA_ROOT / "profiles" / "local-default" / "profile-state.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    (home / "notes").mkdir()
    (home / "notes" / "link").symlink_to(target)

    # when & then
    assert is_denied(home / "notes" / "link", home) is True
