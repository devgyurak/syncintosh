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
from typing import TYPE_CHECKING

import typer

from syncintosh.core.errors import DomainError
from syncintosh.core.tree import path_label
from syncintosh.core.watch import DEFAULT_QUIET_SECONDS
from syncintosh.services import SyncReport
from syncintosh.services import export_ring as export_ring_service
from syncintosh.services import import_ring as import_ring_service
from syncintosh.services import prune_generation as prune_generation_service
from syncintosh.services import remote_commitments as remote_commitments_service
from syncintosh.services import rotate_repo as rotate_repo_service
from syncintosh.services import sync as sync_service

if TYPE_CHECKING:  # both names appear only in a return annotation
    from syncintosh.core.ports import SyntaxValidator
    from syncintosh.core.presets import ValidatorName
    from syncintosh.services.keys import RotationMode

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


def validators() -> dict[ValidatorName, SyntaxValidator]:
    """Every syntax check a preset may name, wired to its implementation.

    A preset names a check by the string in `ValidatorName`; a name with no implementation is
    **refused** at the write, so a missing entry here stops the file from ever being applied on this
    machine. `tests/unit/test_cli.py` compares this map against the literal, so adding a name without
    wiring it fails in CI rather than on a user's machine.
    """
    from syncintosh.system.json_check import JsonSyntaxValidator
    from syncintosh.system.zsh_check import ZshSyntaxValidator

    return {"json": JsonSyntaxValidator(), "zsh": ZshSyntaxValidator()}


# The allowlist's own command group. `scan` itself does nothing; `allow` is the whole surface, kept
# minimal on purpose — the narrow escape hatch issue #25 asks for, not a policy editor.
scan_app = typer.Typer(help="The local, never-synced secret-scan allowlist.", no_args_is_help=True)
app.add_typer(scan_app, name="scan")


@scan_app.command("allow")
def scan_allow(
    path: str = typer.Argument(..., help="the subject a refusal named (a tracked path, or `manifest`)"),
    label: str = typer.Argument(..., help="the pattern label a refusal named"),
    fingerprint: str = typer.Argument(..., help="the fingerprint a refusal printed"),
) -> None:
    """Allow one refused span on this machine only.

    The entry is `(path, label, fingerprint)`, and the fingerprint is keyed, so this stores no
    plaintext. The list never leaves the machine: a synced one would let another machine decide what
    this one does not scan (``AGENTS.md`` §3.3).
    """
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.scan_allow import ScanAllowEntry, load_scan_allow, save_scan_allow

    try:
        allow_file = MachinePaths.default().scan_allow_file
        allow = load_scan_allow(allow_file)
        save_scan_allow(allow.with_entry(ScanAllowEntry(path=path, label=label, fingerprint=fingerprint)), allow_file)
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo(f"allowed on this machine only: {path} ({label}) {fingerprint}")


# The key-lifecycle command group. Rotation, and the out-of-band export/import that hands a
# generation to another machine without publishing a wrap in the repo.
key_app = typer.Typer(help="Rotate, export, and import key generations.", no_args_is_help=True)
app.add_typer(key_app, name="key")


@key_app.command("rotate")
def key_rotate(
    hygiene: bool = typer.Option(False, "--hygiene", help="publish the new key wrapped under the old one (routine)"),
    out_of_band: bool = typer.Option(
        False, "--out-of-band", help="publish no wrap; hand the new key over with `key export`"
    ),
) -> None:
    """Rotate to a new key generation. Mutation is deliberate and rare.

    `--hygiene` is a routine rotation with no known compromise and no machine being removed: the
    new generation is published as `keys/<new_id>.wrap`, wrapped under the previous one, so every
    machine that still holds the old key absorbs it on its next `sync`. `--out-of-band` publishes
    no wrap, so the new key must be handed over with `key export` / `key import`.

    Rotation protects **future** data only: commits already pushed stay decryptable with the old
    key, and a machine that holds the old key keeps its access. On a compromise, rotate out of band
    or migrate to a new repo — do not rely on a rotation to revoke anyone.
    """
    from syncintosh.system.config import load_config
    from syncintosh.system.paths import MachinePaths

    if hygiene == out_of_band:
        raise typer.BadParameter("choose exactly one of --hygiene or --out-of-band")
    mode: RotationMode = "hygiene" if hygiene else "out-of-band"
    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        ports = _sync_ports(config, paths)
        generation = rotate_repo_service(
            transport=ports["transport"],
            files=ports["files"],
            cipher=ports["cipher"],
            wrapper=ports["wrapper"],
            scanner=ports["scanner"],
            store=ports["store"],
            codec=ports["codec"],
            lock=ports["lock"],
            pending=ports["pending_store"],
            remote=config.remote,
            branch=ports["branch"],
            mode=mode,
        )
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo(f"rotated to key generation {generation}")
    if out_of_band:
        typer.echo("hand the new generation over with `syncintosh key export <path>` on each machine")


@key_app.command("export")
def key_export(path: Path = typer.Argument(..., help="where to write the wrapped key ring")) -> None:
    """Write the whole ring to a file, wrapped under a passphrase, for an out-of-band hand-off."""
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.system.config import load_config
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.paths import MachinePaths

    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        export_ring_service(
            store=_key_store(config.remote),
            codec=KeyMaterial(),
            live=SystemLiveFiles(),
            path=str(path),
            passphrase=_passphrase(),
        )
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo(f"wrote the key ring to {path}; keep it somewhere safe")


@key_app.command("import")
def key_import(path: Path = typer.Argument(..., help="a wrapped ring written by `key export`")) -> None:
    """Add the generations in an exported ring, checked against the repo's published commitments."""
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.system.config import load_config
    from syncintosh.system.lock import FileLock
    from syncintosh.system.paths import MachinePaths
    from syncintosh.vcs.git import GitCLITransport

    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        transport = GitCLITransport(paths.repo_dir)
        import_ring_service(
            store=_key_store(config.remote),
            codec=KeyMaterial(),
            blob=path.read_bytes(),
            passphrase=_passphrase(),
            published=remote_commitments_service(transport, config.remote, transport.branch() or BRANCH),
            lock=FileLock(paths.lock_file),
        )
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo(f"imported the key ring from {path}")


@key_app.command("prune")
def key_prune(generation: int = typer.Argument(..., help="the generation id to drop from this machine")) -> None:
    """Drop a key generation no reachable commit uses.

    Reachability frees almost nothing: without rewriting history every commit stays reachable, so
    the only generation this can drop is one that was published but never used — an aborted
    rotation. The master generation 1, and any generation a reachable object or the manifest still
    uses, are refused. Prune does not shrink the ring; to revoke a machine's access, migrate to a
    new repo.
    """
    from syncintosh.system.config import load_config
    from syncintosh.system.paths import MachinePaths

    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        ports = _sync_ports(config, paths)
        prune_generation_service(
            transport=ports["transport"],
            store=ports["store"],
            lock=ports["lock"],
            remote=config.remote,
            branch=ports["branch"],
            generation=generation,
        )
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo(f"generation {generation} is not needed by reachable history")


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
            # The same label the pending-conflict detail uses; an array at the root —
            # `keybindings.json` — is compared whole, so the document itself is the conflict.
            typer.echo(f"conflict: {path_label(conflict.path)}", err=True)
        raise typer.Exit(code=1)


@app.command()
def init(
    remote: str = typer.Option(..., "--remote", help="the encrypted settings repo's git URL"),
    import_file: Path | None = typer.Option(None, "--import", help="a recovery export to import a ring from"),
    recovery: Path | None = typer.Option(None, "--recovery", help="where to write the recovery export"),
    preset: list[str] | None = typer.Option(
        None, "--preset", help="a built-in preset to track; repeat for more than one (default: zsh)"
    ),
) -> None:
    """Create a repo and its first key generation, or import a ring onto a second Mac.

    Every file of every named built-in preset is tracked (`zsh` when none is named). The paths,
    formats, and strip policies come from the catalog, so a typo in a preset name is refused here
    rather than silently tracking nothing (``docs/design/presets.md``).
    """
    from syncintosh.core.config import SyncConfig
    from syncintosh.crypto.file_ids import SecureFileIdSource
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.presets import BuiltinCatalog
    from syncintosh.services import import_ring, init_new_repo
    from syncintosh.services.keys import published_commitments
    from syncintosh.services.setup import mint_machine_id, new_machine_config
    from syncintosh.system.config import save_config
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.lock import FileLock
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.repo_files import FileRepo
    from syncintosh.vcs.git import GitCLITransport

    paths = MachinePaths.default()
    destination = recovery if recovery is not None else paths.home / "syncintosh-recovery.sntr"
    try:
        config: SyncConfig = new_machine_config(
            remote=remote,
            machine_id=mint_machine_id(SecureFileIdSource()),
            preset_names=tuple(preset) if preset else ("zsh",),
            catalog=BuiltinCatalog.default(),
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
                lock=FileLock(paths.lock_file),
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
    from syncintosh.system.config import load_config
    from syncintosh.system.paths import MachinePaths

    if adopt not in (None, "local", "remote"):
        raise typer.BadParameter("--adopt must be `local` or `remote`")
    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        report = sync_service(**_sync_ports(config, paths), adopt=adopt)
    except DomainError as exc:
        _fail(exc)
        return
    _render(report, config)
    settled = (*report.refused, *report.unprojectable, *report.invalid)
    if report.has_conflicts or report.needs_adopt or any(refusal.blocking for refusal in settled):
        # A path that was refused, could not be projected, or would not parse is a failure, not a
        # partial success: a launchd job or a script must not read "nothing was applied here" as
        # "all is well". A record this machine simply does not track is *not* that — it is a normal
        # state (every machine carries every record) and is reported on stdout instead.
        raise typer.Exit(code=1)


def _sync_ports(config, paths) -> dict:
    """Every port one `sync` cycle needs, from this machine's paths and config.

    Shared by `sync` and `watch`, so the two cannot drift on which catalog, scanner, or adapter map
    they wire (``AGENTS.md`` §7). The imports stay inside, so CLI startup does not pay for a heavy
    dependency (``AGENTS.md`` §4).
    """
    from syncintosh.adapters.jsonc import JsoncAdapter
    from syncintosh.adapters.text_merge import TextMergeDriver
    from syncintosh.crypto.cipher import EnvelopeCipher
    from syncintosh.crypto.file_ids import SecureFileIdSource
    from syncintosh.crypto.key_material import KeyMaterial
    from syncintosh.presets import BuiltinCatalog
    from syncintosh.system.app_detector import MdfindAppDetector
    from syncintosh.system.deny_list import SystemDenyList
    from syncintosh.system.ledger import JsonLedgerStore
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.lock import FileLock
    from syncintosh.system.pending import DirectoryPendingStore
    from syncintosh.system.repo_files import FileRepo
    from syncintosh.system.scan_allow import load_scan_allow
    from syncintosh.system.secret_scan import PatternSecretScanner
    from syncintosh.vcs.git import GitCLITransport

    store = _key_store(config.remote)
    transport = GitCLITransport(paths.repo_dir)
    # One object is both the `Cipher` and the `GenerationWrap`: the fetch absorbs a distributed
    # generation through the same ring the objects are opened with.
    cipher = EnvelopeCipher(store)
    return {
        "transport": transport,
        "files": FileRepo(paths.repo_dir),
        "cipher": cipher,
        "wrapper": cipher,
        # The local allowlist lets a user pass a line the heuristic cannot classify (#25); it lives
        # outside the repo, so it never leaves this machine.
        "scanner": PatternSecretScanner(load_scan_allow(paths.scan_allow_file)),
        "store": store,
        "codec": KeyMaterial(),
        "driver": TextMergeDriver(),
        "adapters": {"jsonc": JsoncAdapter()},
        "catalog": BuiltinCatalog.default(),
        "validators": validators(),
        "live": SystemLiveFiles(),
        "ledger_store": JsonLedgerStore(paths.ledger_file),
        "pending_store": DirectoryPendingStore(paths.pending_dir),
        "lock": FileLock(paths.lock_file),
        "deny": SystemDenyList(),
        "apps": MdfindAppDetector(),
        "source": SecureFileIdSource(),
        "config": config,
        "home": str(paths.home),
        "branch": transport.branch() or BRANCH,
    }


@app.command()
def watch(
    quiet: float = typer.Option(
        DEFAULT_QUIET_SECONDS, "--quiet", help="seconds of quiet before a change triggers a sync"
    ),
) -> None:
    """Watch the tracked paths and sync after a change settles. Runs in the foreground.

    Registering this with launchd and running it on a schedule is #8; this is the loop itself.
    """
    from syncintosh.core.watch import WatchFilter
    from syncintosh.services import watch as watch_service
    from syncintosh.system.config import load_config
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.watcher import WatchdogFileWatch

    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        ports = _sync_ports(config, paths)
    except DomainError as exc:
        _fail(exc)
        return
    targets = WatchFilter.create(config)

    def run() -> SyncReport:
        return sync_service(**ports)

    def error(exc: Exception) -> None:
        # Anything a **cycle** can fail with — another sync's lock, a pending conflict, a network
        # failure, a file that cannot be read — is reported and recovered from; the loop only raises
        # when the watch could not start at all (``services/watch.py``).
        typer.echo(f"watch: {exc}", err=True)

    watcher = WatchdogFileWatch(home=str(paths.home), directories=targets.directories())
    typer.echo(
        f"watching {', '.join(targets.directories()) or 'nothing (no tracked paths)'}"
        "; a directory that does not exist yet is picked up when it appears"
    )
    try:
        watch_service(
            watch=watcher,
            targets=targets,
            run=run,
            quiet_seconds=quiet,
            on_report=lambda report: _render(report, config),
            on_error=error,
        )
    except KeyboardInterrupt:
        typer.echo("stopped watching")
    except DomainError as exc:
        # The loop recovers from every cycle failure itself, so the one it raises is a watch that
        # could not start: a hard failure, rendered exactly as `sync` renders one rather than as a
        # traceback.
        _fail(exc)


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
        ("suppressed (removed here)", report.suppressed),
        ("skipped (dangerous path)", report.skipped),
        ("skipped (symbolic link)", report.symlinks),
        ("aborted (edited during sync; re-sync)", report.aborted),
    ):
        for path in paths:
            typer.echo(f"{label}: {path}")
    for deferral in report.deferred:
        # A deferral is not a failure (the next sync on a machine that has the app writes it), so it
        # goes to stdout, with the app's own download page when that is why it was deferred.
        typer.echo(f"deferred ({deferral.reason}): {deferral.path}")
        if deferral.download_url:
            typer.echo(f"  download: {deferral.download_url}")
        if deferral.cask:
            typer.echo(f"  or: brew install --cask {deferral.cask}")
    for refusal in report.refused:
        if refusal.blocking:
            # The reason is a fixed phrase, never file content (``AGENTS.md`` §4).
            typer.echo(f"refused ({refusal.reason}): {refusal.path}", err=True)
        else:
            # Not `skipped`: that word already means a dangerous path here, and the two call for very
            # different reactions. The reason is shown too, so "the glob is full" is not mistaken for
            # "this machine does not track the path".
            typer.echo(f"not synced ({refusal.reason}): {refusal.path}")
    for label, refusals in (("unprojectable", report.unprojectable), ("invalid", report.invalid)):
        for refusal in refusals:
            typer.echo(f"{label} ({refusal.reason}): {refusal.path}", err=True)
    if report.unchanged:
        typer.echo("nothing changed")
    elif report.published_commit:
        typer.echo(f"published {report.published_commit[:12]}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
