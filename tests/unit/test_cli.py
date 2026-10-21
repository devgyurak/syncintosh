# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the CLI: parsing, delegation, rendering, and exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from typer.testing import CliRunner

from syncintosh.cli import main as main_module
from syncintosh.core.presets import ValidatorName
from syncintosh.presets import BuiltinCatalog
from syncintosh.services import Refusal, SyncReport

runner = CliRunner()


def _write(directory: Path, name: str, payload: object) -> str:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _combined(result) -> str:
    return (result.stdout or "") + (getattr(result, "stderr", "") or "")


def test_merge_prints_the_merged_object(tmp_path) -> None:
    # given
    base = _write(tmp_path, "base.json", {"a": "1"})
    local = _write(tmp_path, "local.json", {"a": "2"})
    remote = _write(tmp_path, "remote.json", {"a": "1"})

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"a": "2"}


def test_merge_exits_1_and_names_the_conflict(tmp_path) -> None:
    # given
    base = _write(tmp_path, "base.json", {"a": "1"})
    local = _write(tmp_path, "local.json", {"a": "2"})
    remote = _write(tmp_path, "remote.json", {"a": "3"})

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then
    assert result.exit_code == 1
    assert "conflict: a" in _combined(result)


def test_merge_rejects_an_unreadable_file(tmp_path) -> None:
    # given
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    good = _write(tmp_path, "good.json", {})

    # when
    result = runner.invoke(main_module.app, ["merge", str(bad), good, good])

    # then
    assert result.exit_code == 2


def test_merge_accepts_a_top_level_array(tmp_path) -> None:
    # given — a document whose root is an array
    base = _write(tmp_path, "base.json", [1, 2])
    local = _write(tmp_path, "local.json", [1, 2, 3])
    remote = _write(tmp_path, "remote.json", [1, 2])

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then
    assert result.exit_code == 0
    assert json.loads(result.stdout) == [1, 2, 3]


def test_main_dispatches_to_the_app(monkeypatch) -> None:
    # given
    calls: dict[str, int] = {}
    monkeypatch.setattr(main_module, "app", lambda: calls.setdefault("count", 0))

    # when
    main_module.main()

    # then
    assert calls == {"count": 0}


def test_sync_rejects_an_unknown_adopt_choice() -> None:
    # when
    result = runner.invoke(main_module.app, ["sync", "--adopt", "both"])

    # then
    assert result.exit_code == 2


def test_sync_without_a_configuration_reports_an_error() -> None:
    # when
    result = runner.invoke(main_module.app, ["sync"])

    # then
    assert result.exit_code == 1
    assert "error:" in _combined(result)


def test_importing_the_cli_does_not_import_a_heavy_dependency() -> None:
    # given — a fresh interpreter, so the modules other tests imported are absent
    code = (
        "import sys, syncintosh.cli.main; "
        "print(','.join(m for m in ('cryptography', 'lxml', 'openai') if m in sys.modules))"
    )

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then — CLI startup stays fast (AGENTS.md §4)
    assert result.stdout.strip() == ""


def test_importing_the_cli_does_not_import_the_jsonc_adapter() -> None:
    # given — the jsonc adapter is only needed inside `merge`/`sync`, not at import time
    code = "import sys, syncintosh.cli.main; print('syncintosh.adapters.jsonc' in sys.modules)"

    # when
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    # then
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            SyncReport(unprojectable=(Refusal(".zshrc", "the file could not be reduced to its synced form"),)),
            "unprojectable (the file could not be reduced to its synced form): .zshrc",
        ),
        (SyncReport(invalid=(Refusal(".zshrc", "the file would not pass its syntax check"),)), "invalid ("),
        (SyncReport(refused=(Refusal(".zshrc", "the path's preset does not own it"),)), "refused ("),
    ],
)
def test_sync_exits_1_for_a_path_it_could_not_write(report, expected, tmp_path, monkeypatch) -> None:
    # given — the service reports a path it refused, could not project, or would not write
    from syncintosh.core.config import LocalItem, SyncConfig
    from syncintosh.system.config import save_config
    from syncintosh.system.paths import MachinePaths

    paths = MachinePaths.default()
    item = LocalItem.create(path=".zshrc", preset="zsh", scope="common", format="text")
    save_config(
        SyncConfig.create(remote="git@example.test:settings.git", machine_id="machine-a", items=(item,)),
        paths.config_file,
    )
    monkeypatch.setattr("syncintosh.vcs.git.GitCLITransport.branch", lambda _self: "main")
    monkeypatch.setattr(main_module, "sync_service", lambda **_kwargs: report)

    # when
    result = runner.invoke(main_module.app, ["sync"])

    # then — the exit code says "not applied", and the reason is named rather than left to guess
    assert result.exit_code == 1
    assert expected in _combined(result)


def test_sync_exits_0_for_a_record_this_machine_does_not_track(tmp_path, monkeypatch) -> None:
    # given — the service reports a record this machine's config does not name, which is ordinary:
    # every machine carries every record, and one set up with fewer presets is not broken
    from syncintosh.core.config import LocalItem, SyncConfig
    from syncintosh.system.config import save_config
    from syncintosh.system.paths import MachinePaths

    paths = MachinePaths.default()
    item = LocalItem.create(path=".zshrc", preset="zsh", scope="common", format="text")
    save_config(
        SyncConfig.create(remote="git@example.test:settings.git", machine_id="machine-a", items=(item,)),
        paths.config_file,
    )
    monkeypatch.setattr("syncintosh.vcs.git.GitCLITransport.branch", lambda _self: "main")
    monkeypatch.setattr(
        main_module,
        "sync_service",
        lambda **_kwargs: SyncReport(
            refused=(
                Refusal(
                    "Library/Application Support/Code/User/settings.json",
                    "the path is not in this machine's config",
                    blocking=False,
                ),
            )
        ),
    )

    # when
    result = runner.invoke(main_module.app, ["sync"])

    # then — reported on stdout as its own case (not the word a dangerous path gets), and the exit
    # code says the cycle did its job
    assert result.exit_code == 0
    assert "not tracked here: Library/Application Support/Code/User/settings.json" in _combined(result)


def test_every_validator_name_is_wired_to_an_implementation() -> None:
    # when — a preset names its syntax check by the string in `ValidatorName`
    wired = main_module.validators()
    declared = get_args(ValidatorName)

    # then — each name this build declares has something to run it. A name with no implementation is
    # **refused** at the write, so a missing entry here stops every file that names it from ever
    # being applied, and nothing on the machine would say why.
    assert sorted(wired) == sorted(declared)


def test_every_preset_validator_is_wired() -> None:
    # when — the checks the shipped presets actually ask for
    used = {
        file.validator
        for preset in BuiltinCatalog.default().presets
        for file in preset.files
        if file.validator is not None
    }

    # then
    assert used - set(main_module.validators()) == set()


def test_merge_names_a_whole_document_conflict(tmp_path) -> None:
    # given — an array at the root, compared whole, exactly the shape of `keybindings.json`
    base = _write(tmp_path, "base.json", [1])
    local = _write(tmp_path, "local.json", [2])
    remote = _write(tmp_path, "remote.json", [3])

    # when
    result = runner.invoke(main_module.app, ["merge", base, local, remote])

    # then — there is no key to name, so the document itself is named
    assert result.exit_code == 1
    assert "conflict: (document)" in _combined(result)


def test_scan_allow_records_an_entry_against_the_current_key(tmp_path) -> None:
    # given — the fingerprint the refusal printed must be recorded against the key that produced it,
    # or `scan allow` would store an entry the scanner can never match
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.scan_allow import fingerprint, load_scan_allow

    allow_file = MachinePaths.default().scan_allow_file
    matched = b"http://me:s3cr;t@proxy:8080"
    digest = fingerprint(load_scan_allow(allow_file).key, matched)

    # when
    result = runner.invoke(main_module.app, ["scan", "allow", ".zshrc", "URL with credentials", digest])

    # then
    assert result.exit_code == 0
    assert load_scan_allow(allow_file).allows(".zshrc", "URL with credentials", digest)


def test_scan_allow_rejects_a_malformed_fingerprint(tmp_path) -> None:
    # given — a mistyped fingerprint must not be written, or every later load would fail (and
    # deleting the file would mint a new key, silently invalidating the recorded fingerprints)
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.scan_allow import load_scan_allow

    allow_file = MachinePaths.default().scan_allow_file

    # when
    result = runner.invoke(main_module.app, ["scan", "allow", ".zshrc", "URL with credentials", "NOTHEX"])

    # then — the command failed and left the list loadable and empty
    assert result.exit_code == 1
    assert "error:" in _combined(result)
    assert load_scan_allow(allow_file).entries == frozenset()


def test_scan_allow_reports_a_corrupt_allowlist(tmp_path) -> None:
    # given — a hand-edited file that no longer parses must fail loudly, not silently reset
    from syncintosh.system.paths import MachinePaths

    allow_file = MachinePaths.default().scan_allow_file
    allow_file.parent.mkdir(parents=True, exist_ok=True)
    allow_file.write_text("{not json", encoding="utf-8")

    # when
    result = runner.invoke(main_module.app, ["scan", "allow", ".zshrc", "URL with credentials", "a" * 32])

    # then
    assert result.exit_code == 1
    assert "error:" in _combined(result)
