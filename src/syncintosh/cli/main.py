# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `syncintosh` command-line entry point.

The binary is `syncintosh` (alias `snt`). Commands stay thin: parse, delegate to a service,
render (``AGENTS.md`` §2.3). All judgment lives in `core`/`services`.

Heavy dependencies (`cryptography`, `lxml`, `openai`) are imported **inside** the command
functions, so importing this module keeps CLI startup fast (``AGENTS.md`` §4).
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from syncintosh.core.errors import DomainError
from syncintosh.services import SyncReport
from syncintosh.services import sync as sync_service

app = typer.Typer(
    help="Keep developer-environment settings in step across Macs.",
    no_args_is_help=True,
    add_completion=False,
)

# The one branch syncintosh uses. One branch keeps the merge-base model simple.
BRANCH = "main"

# Environment override for non-interactive runs (tests, the daemon). A passphrase on argv would
# land in shell history, so it is read from the environment or prompted, never from an option.
PASSPHRASE_ENV = "SYNCINTOSH_PASSPHRASE"


@app.callback()
def _root() -> None:
    """syncintosh — keep developer-environment settings in step across Macs.

    A callback keeps the app a real command group. Without it Typer collapses a
    single-command app, so `syncintosh merge …` would treat `merge` as the first argument.
    """


def _passphrase() -> str:
    """The recovery passphrase, from the environment or an interactive prompt."""
    from_env = os.environ.get(PASSPHRASE_ENV)
    if from_env:
        return from_env
    return typer.prompt("Recovery passphrase", hide_input=True, confirmation_prompt=True)


def _key_store(remote: str):
    """The per-repo key ring, namespaced so two repos on one Mac do not share keys."""
    from syncintosh.crypto.keyring_store import KeyringKeyStore

    return KeyringKeyStore(f"syncintosh:{remote}")


def _fail(exc: DomainError) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=1)


@app.command()
def merge(base: Path, local: Path, remote: Path) -> None:
    """Three-way merge of three JSONC files, preserving comments. Exits 1 on conflicts.

    The merged tree is rendered against the local file, so comments, a trailing comma, and the
    key order survive and only a changed value's span is rewritten. The adapter stays a lazy
    import so CLI startup does not pay for JSONC parsing (``AGENTS.md`` §4).
    """
    from syncintosh.adapters.jsonc import JsoncAdapter
    from syncintosh.core.errors import ContentParseError
    from syncintosh.services import merge_text

    try:
        base_text = base.read_text(encoding="utf-8")
        local_text = local.read_text(encoding="utf-8")
        remote_text = remote.read_text(encoding="utf-8")
        result = merge_text(base_text, local_text, remote_text, JsoncAdapter())
    except (OSError, UnicodeDecodeError, ContentParseError) as exc:
        raise typer.BadParameter(f"cannot read JSONC from {base} / {local} / {remote}: {exc}") from exc
    typer.echo(result.text, nl=False)
    if result.has_conflicts:
        for conflict in result.conflicts:
            typer.echo(f"conflict: {'.'.join(conflict.path)}", err=True)
        raise typer.Exit(code=1)


@app.command()
def init(
    remote: str = typer.Option(..., "--remote", help="the encrypted settings repo's git URL"),
    import_file: Path | None = typer.Option(None, "--import", help="a recovery export to import a ring from"),
    recovery: Path | None = typer.Option(None, "--recovery", help="where to write the recovery export"),
    path: str = typer.Option(".zshrc", "--path", help="the tracked path, relative to $HOME"),
    preset: str = typer.Option("zsh", "--preset", help="the preset that owns the tracked path"),
) -> None:
    """Create a repo and its first key generation, or import a ring onto a second Mac.

    `--path`/`--preset` name the single tracked text file. The real preset catalog is #6; until
    then the configuration carries one local item (``docs/design/presets.md``).
    """
    from syncintosh.core.config import SyncConfig
    from syncintosh.crypto.file_ids import SecureFileIdSource
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.services import import_ring, init_new_repo
    from syncintosh.services.keys import published_commitments
    from syncintosh.services.setup import mint_machine_id, new_machine_config
    from syncintosh.system.config import save_config
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.repo_files import FileRepo
    from syncintosh.vcs.git import GitCLITransport

    paths = MachinePaths.default()
    destination = recovery if recovery is not None else paths.home / "syncintosh-recovery.sntr"
    try:
        config: SyncConfig = new_machine_config(
            remote=remote,
            machine_id=mint_machine_id(SecureFileIdSource()),
            path=path,
            preset=preset,
        )
        transport = GitCLITransport(paths.repo_dir)
        store = _key_store(remote)
        codec = KeyMaterial()
        if import_file is not None:
            transport.clone(remote)
            import_ring(
                store=store,
                codec=codec,
                blob=import_file.read_bytes(),
                passphrase=_passphrase(),
                published=published_commitments(transport),
            )
            typer.echo(f"imported the key ring from {import_file}")
        else:
            transport.init()
            init_new_repo(
                store=store,
                codec=codec,
                files=FileRepo(paths.repo_dir),
                live=SystemLiveFiles(),
                recovery_path=str(destination),
                passphrase=_passphrase(),
            )
            typer.echo(f"initialized; keep the recovery export at {destination} somewhere safe")
        save_config(config, paths.config_file)
    except DomainError as exc:
        _fail(exc)


@app.command()
def sync(adopt: str | None = typer.Option(None, "--adopt", help="local | remote, for a first-sight file")) -> None:
    """Collect, merge, publish, and apply. Exits 1 when conflicts remain."""
    from syncintosh.adapters.jsonc import JsoncAdapter
    from syncintosh.adapters.text import TextAdapter
    from syncintosh.crypto.cipher import EnvelopeCipher
    from syncintosh.crypto.file_ids import SecureFileIdSource
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.system.config import load_config
    from syncintosh.system.deny_list import SystemDenyList
    from syncintosh.system.ledger import JsonLedgerStore
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.lock import FileLock
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.pending import DirectoryPendingStore
    from syncintosh.system.repo_files import FileRepo
    from syncintosh.system.secret_scan import PatternSecretScanner
    from syncintosh.vcs.git import GitCLITransport

    if adopt not in (None, "local", "remote"):
        raise typer.BadParameter("--adopt must be `local` or `remote`")
    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        store = _key_store(config.remote)
        transport = GitCLITransport(paths.repo_dir)
        report = sync_service(
            transport=transport,
            files=FileRepo(paths.repo_dir),
            cipher=EnvelopeCipher(store),
            scanner=PatternSecretScanner(),
            store=store,
            codec=KeyMaterial(),
            driver=TextAdapter(),
            adapters={"jsonc": JsoncAdapter()},
            live=SystemLiveFiles(),
            ledger_store=JsonLedgerStore(paths.ledger_file),
            pending_store=DirectoryPendingStore(paths.pending_dir),
            lock=FileLock(paths.lock_file),
            deny=SystemDenyList(),
            source=SecureFileIdSource(),
            config=config,
            home=str(paths.home),
            branch=transport.branch() or BRANCH,
            adopt=adopt,
        )
    except DomainError as exc:
        _fail(exc)
        return
    _render(report, config)
    if report.has_conflicts or report.needs_adopt:
        raise typer.Exit(code=1)


def _render(report: SyncReport, config) -> None:
    """Print the cycle's outcome; the wording is the user's only account of what happened."""
    if report.first_sight:
        typer.echo("these files exist here and in the repo with different content:")
        for path in report.first_sight:
            typer.echo(f"  {path}")
        typer.echo("re-run with `sync --adopt local` (keep these) or `sync --adopt remote` (take the repo's)")
    if report.conflicts:
        typer.echo("conflicts stopped the sync; nothing was applied:", err=True)
        for conflict in report.conflicts:
            typer.echo(f"  {conflict.path or conflict.file_id}: {conflict.detail}", err=True)
    for label, paths in (
        ("created", report.created),
        ("collected", report.collected),
        ("applied", report.applied),
        ("deferred (app not installed)", report.deferred),
        ("suppressed (removed here)", report.suppressed),
        ("skipped (dangerous path)", report.skipped),
        ("refused (not a tracked path here)", report.refused),
        ("skipped (symbolic link)", report.symlinks),
        ("aborted (edited during sync; re-sync)", report.aborted),
    ):
        for path in paths:
            typer.echo(f"{label}: {path}")
    if report.unchanged:
        typer.echo("nothing changed")
    elif report.published_commit:
        typer.echo(f"published {report.published_commit[:12]}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
