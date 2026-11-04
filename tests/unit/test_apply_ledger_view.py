# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit test: apply measures a file with carried keys against the ledger's object when collect did not see it.

With no collect baseline, `_decision` compares the live view with the ledger digest. That digest is of the
ledger's object, so a remote change to a carried key must not read as a local edit
(``docs/design/02-base-and-merge.md``).
"""

from __future__ import annotations

import hashlib
import json
from typing import cast

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.config import LocalItem, SyncConfig
from syncintosh.core.ignore import IGNORED_SETTINGS_KEY, IgnoreSpec
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.models import LedgerEntry
from syncintosh.core.ports import Cipher, GitTransport
from syncintosh.core.repo import object_path
from syncintosh.presets import BuiltinCatalog, vscode
from syncintosh.services.policy_gate import PolicyView
from syncintosh.services.sync import NO_APP_INFO, Backup, _apply, _Presets
from syncintosh.system.deny_list import SystemDenyList
from syncintosh.system.live_files import SystemLiveFiles

FILE_ID = "1" * 32
OLD = (json.dumps({"editor.tabSize": 1, "editor.fontSize": 1}, indent=2) + "\n").encode()
NEW = (json.dumps({"editor.tabSize": 2, "editor.fontSize": 2}, indent=2) + "\n").encode()


class _Transport:
    """Serves one object per commit; everything else is unused by apply's reads."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def read_blob(self, commit: str, relpath: str) -> bytes | None:
        return self._objects.get(commit) if relpath == object_path(FILE_ID) else None


class _Plain:
    """A `Cipher` whose sealed form is the plaintext, so the test is about which object is read."""

    def open(self, blob: bytes, *, domain, file_id: str) -> bytes:
        return blob


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_remote_change_to_a_carried_key_does_not_abort_a_file_collect_never_saw(tmp_path) -> None:
    # given — the ledger accounts for OLD; the repo moved on to NEW in the tab size *and* the ignored font size;
    # this machine edited neither tab size nor the ignored key's meaning, only holds its own font size
    target = tmp_path / vscode.SETTINGS
    target.parent.mkdir(parents=True)
    live = (json.dumps({"editor.tabSize": 1, "editor.fontSize": 99}, indent=2) + "\n").encode()
    target.write_bytes(live)
    record = ManifestRecord.create(
        file_id=FILE_ID, path=vscode.SETTINGS, preset="vscode", scope="common", format="jsonc", sha256=_sha(NEW)
    )
    manifest = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={FILE_ID: record})
    presets = _Presets(
        catalog=BuiltinCatalog.default(),
        adapters={"jsonc": JsoncAdapter()},
        validators={},
        ignores={vscode.SETTINGS: IgnoreSpec.create({IGNORED_SETTINGS_KEY: ["editor.fontSize"]})},
    )
    config = SyncConfig.create(
        remote="r",
        machine_id="m",
        items=(LocalItem.create(path=vscode.SETTINGS, preset="vscode", scope="common", format="jsonc"),),
    )

    # when
    applied = _apply(
        transport=PolicyView(
            cast(GitTransport, _Transport({"old": OLD, "new": NEW})),
            cipher=cast(Cipher, _Plain()),
            catalog=presets.catalog,
            project=lambda data, _path, _fmt: data,
            scanner=None,
        ),
        cipher=cast(Cipher, _Plain()),
        presets=presets,
        target_commit="new",
        target_manifest=manifest,
        seen={},
        views={},
        ledger={FILE_ID: LedgerEntry(sha256=_sha(OLD), commit="old", suppressed=False)},
        live=SystemLiveFiles(),
        deny=SystemDenyList(),
        apps=NO_APP_INFO,
        config=config,
        home=str(tmp_path),
        adopt=None,
        backup=Backup.create("new"),
    )

    # then — the remote tab size is applied and this machine keeps its font size
    assert (applied.aborted, applied.applied) == ((), (vscode.SETTINGS,))
    assert json.loads(target.read_text(encoding="utf-8")) == {"editor.tabSize": 2, "editor.fontSize": 99}
