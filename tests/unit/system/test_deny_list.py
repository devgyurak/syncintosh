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
