# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a second JetBrains product across two Macs on two releases (issue #54, PR D).

`test_datagrip_preset.py` covers the whole flow for DataGrip; the other products are the same builder with other data,
so this runs one of them end to end per prefix shape (`RustRover`: a lowercase bundle ID; `IntelliJIdea`: a prefix that
is not the product's name) to prove the logical root, the release folder and the apply follow the product's own data.
The installed app is a temp `.app` bundle with a real-shaped `product-info.json`; no network, no real HOME.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_datagrip_preset import (
    FIXTURES,
    HOME,
    FakeApps,
    FakeRunning,
    Machine,
    _bare_remote,
    _initialize,
    _records,
    _write,
)

from syncintosh.adapters.text_merge import TextMergeDriver
from syncintosh.adapters.xml import XmlAdapter
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.file_ids import SecureFileIdSource
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.sync import SyncPorts, SyncReport, sync
from syncintosh.system.app_config_dir import ProductInfoConfigDir
from syncintosh.system.config import load_config
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.ledger import JsonLedgerStore
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.lock import FileLock
from syncintosh.system.pending import DirectoryPendingStore
from syncintosh.system.repo_files import FileRepo
from syncintosh.system.secret_scan import PatternSecretScanner
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

CATALOG = BuiltinCatalog.default()
BRANCH = "main"

# (preset name, folder prefix, app bundle name)
PRODUCTS = [("rustrover", "RustRover", "RustRover.app"), ("intellij-idea", "IntelliJIdea", "IntelliJ IDEA.app")]


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _machine(
    tmp_path: Path, make_store: Callable[[], Any], name: str, remote: str, release: str, app_name: str
) -> Machine:
    home = tmp_path / name / "home"
    state = home / ".local" / "share" / "syncintosh"
    app = tmp_path / name / "apps" / app_name
    (app / "Contents" / "Resources").mkdir(parents=True)
    info = {"name": app_name, "dataDirectoryName": release, "productVendor": "JetBrains"}
    (app / "Contents" / "Resources" / "product-info.json").write_text(json.dumps(info), encoding="utf-8")
    return Machine(
        home=home,
        repo=state / "repo",
        ledger=state / "applied.json",
        pending=state / "pending",
        lock=state / "sync.lock",
        config_file=home / ".config" / "syncintosh" / "config.json",
        store=make_store(),
        remote=remote,
        machine_id=name,
        release=release,
        app=app,
    )


def _run(machine: Machine) -> SyncReport:
    active = GitCLITransport(machine.repo)
    cipher = EnvelopeCipher(machine.store)
    return sync(
        SyncPorts.create(
            transport=active,
            files=FileRepo(machine.repo),
            cipher=cipher,
            scanner=PatternSecretScanner(),
            store=machine.store,
            codec=machine.codec,
            wrapper=cipher,
            driver=TextMergeDriver(),
            adapters={"xml": XmlAdapter()},
            catalog=CATALOG,
            live=SystemLiveFiles(),
            ledger_store=JsonLedgerStore(machine.ledger),
            pending_store=DirectoryPendingStore(machine.pending),
            lock=FileLock(machine.lock, timeout_seconds=5),
            deny=SystemDenyList(),
            apps=FakeApps(),
            running=FakeRunning(False),
            source=SecureFileIdSource(),
            config=load_config(machine.config_file),
            home=str(machine.home),
            branch=active.branch() or BRANCH,
            config_dirs=ProductInfoConfigDir(lambda _bundle: (str(machine.app),), home=machine.home),
        )
    )


def _folder(machine: Machine) -> Path:
    return machine.home / HOME / machine.release


def _pair(tmp_path: Path, make_store: Callable[[], Any], product: tuple[str, str, str]) -> tuple[Machine, Machine]:
    name, prefix, app_name = product
    remote = _bare_remote(tmp_path)
    a = _machine(tmp_path, make_store, "machine-a", remote, f"{prefix}2026.2", app_name)
    _initialize(a, presets=(name,))
    _write(_folder(a) / "options" / "editor-font.xml", _fixture("editor-font.xml"))
    _write(_folder(a) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml"))
    _write(_folder(a) / "colors" / "sample.icls", _fixture("sample.icls"))
    _run(a)
    b = _machine(tmp_path, make_store, "machine-b", remote, f"{prefix}2026.3", app_name)
    _initialize(b, import_from=a.recovery, presets=(name,))
    _folder(b).mkdir(parents=True)
    return a, b


@pytest.mark.parametrize("product", PRODUCTS, ids=lambda product: product[0])
def test_two_macs_on_two_releases_converge_on_the_products_own_logical_root(
    tmp_path, memory_key_store, product: tuple[str, str, str]
) -> None:
    # given
    _, prefix, _ = product
    a, b = _pair(tmp_path, memory_key_store, product)

    # when
    report = _run(b)

    # then — B's own release folder receives the files, and every record sits under this product's logical root
    assert report.has_conflicts is False
    assert (_folder(b) / "options" / "editor-font.xml").read_bytes() == _fixture("editor-font.xml")
    # the scheme's metaInfo is this machine's own noise, so only the shared colours are compared
    assert b"ABCDEF" in (_folder(b) / "colors" / "sample.icls").read_bytes()
    assert _records(b) == _records(a)
    assert sorted(_records(b)) == sorted(
        f"{HOME}/@{prefix}/{relative}"
        for relative in ("options/editor-font.xml", "options/mac/keymap.xml", "colors/sample.icls")
    )


@pytest.mark.parametrize("product", PRODUCTS, ids=lambda product: product[0])
def test_an_edit_on_the_newer_release_reaches_the_older_one(
    tmp_path, memory_key_store, product: tuple[str, str, str]
) -> None:
    # given
    a, b = _pair(tmp_path, memory_key_store, product)
    _run(b)

    # when
    _write(_folder(b) / "options" / "mac" / "keymap.xml", _fixture("keymap.xml").replace(b"Sample Keymap", b"Other"))
    _run(b)
    _run(a)

    # then
    assert b"Other" in (_folder(a) / "options" / "mac" / "keymap.xml").read_bytes()
