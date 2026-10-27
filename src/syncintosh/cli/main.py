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
from urllib.parse import urlsplit

import typer
from rich.console import Console

from syncintosh.core.errors import DomainError
from syncintosh.core.ports import WhenTag
from syncintosh.core.tree import path_label
from syncintosh.core.watch import DEFAULT_QUIET_SECONDS
from syncintosh.services import SyncReport
from syncintosh.services import diff_repo as diff_repo_service
from syncintosh.services import doctor as doctor_service
from syncintosh.services import export_ring as export_ring_service
from syncintosh.services import import_ring as import_ring_service
from syncintosh.services import prune_generation as prune_generation_service
from syncintosh.services import remote_commitments as remote_commitments_service
from syncintosh.services import rotate_repo as rotate_repo_service
from syncintosh.services import sync as sync_service
from syncintosh.system.daemon import DEFAULT_POLL_SECONDS

if TYPE_CHECKING:  # these names appear only in annotations
    from collections.abc import Callable

    from syncintosh.core.ports import SyntaxValidator
    from syncintosh.core.presets import ValidatorName
    from syncintosh.core.resolution import Choice, ConflictView, NamedChoice, Picks
    from syncintosh.services.keys import RotationMode
    from syncintosh.services.resolve import Suggestion

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

# Rich renders the reports. `soft_wrap` and `markup=False` keep the printed text exactly the string
# passed in — never wrapped or read as markup — so a report is the same whether or not a terminal
# is attached (``AGENTS.md`` §2.3).
_CONSOLE = Console(soft_wrap=True, markup=False, emoji=False)
_CONSOLE_ERR = Console(stderr=True, soft_wrap=True, markup=False, emoji=False)


def _say(message: str, *, err: bool = False) -> None:
    """Print one report line through Rich, unchanged."""
    (_CONSOLE_ERR if err else _CONSOLE).print(message, highlight=False)


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


def _launchd_daemon(home):
    """The launchd daemon for `home`. A seam, so a test drives the commands without `launchctl`."""
    from syncintosh.system.daemon import LaunchdDaemon

    return LaunchdDaemon(home)


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


# The launchd job group. `install` recurs every time so the plist always embeds the current build.
daemon_app = typer.Typer(help="Install and remove the background sync job.", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("install")
def daemon_install(
    poll: int = typer.Option(DEFAULT_POLL_SECONDS, "--poll", min=1, help="seconds between remote-polling syncs"),
) -> None:
    """Install (or refresh) the launchd job that runs `syncintosh watch` in the background.

    The plist embeds this build's **absolute** entry point and is rewritten every install, so a
    stale path left by a different Python or install method cannot survive. `syncintosh doctor`
    verifies it.
    """
    from syncintosh.system.daemon import entry_point
    from syncintosh.system.paths import MachinePaths

    entry = entry_point()
    if entry is None:
        raise typer.BadParameter("`syncintosh` is not on PATH; install it before the daemon")
    try:
        _launchd_daemon(MachinePaths.default().home).install(entry_point=entry, poll_seconds=poll)
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo(f"installed the background job; it runs {entry} watch --poll {poll}")


@daemon_app.command("uninstall")
def daemon_uninstall() -> None:
    """Stop the launchd job and remove its plist."""
    from syncintosh.system.paths import MachinePaths

    try:
        _launchd_daemon(MachinePaths.default().home).uninstall()
    except DomainError as exc:
        _fail(exc)
        return
    typer.echo("removed the background job")


@app.command()
def doctor() -> None:
    """Check that this machine's install can actually sync. Exits 1 when a check fails."""
    from syncintosh.system.config import load_config
    from syncintosh.system.daemon import entry_point
    from syncintosh.system.lock import FileLock
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.pending import DirectoryPendingStore
    from syncintosh.vcs.git import GitCLITransport

    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        transport = GitCLITransport(paths.repo_dir)
        daemon = _launchd_daemon(paths.home)
        report = doctor_service(
            transport=transport,
            store=_key_store(config.remote),
            # A short wait: doctor asks whether the lock is held, not to take it for a long while.
            lock=FileLock(paths.lock_file, timeout_seconds=0.5),
            pending=DirectoryPendingStore(paths.pending_dir),
            remote=config.remote,
            branch=transport.branch() or BRANCH,
            plist_entry_point=daemon.recorded_entry_point(),
            expected_entry_point=entry_point(),
            job_loaded=daemon.is_loaded(),
        )
    except DomainError as exc:
        _fail(exc)
        return
    for check in report.checks:
        typer.echo(f"[{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    if not report.healthy:
        raise typer.Exit(code=1)


@app.command()
def diff(values: bool = typer.Option(False, "--values", help="also show a unified diff of the changed values")) -> None:
    """Show how this machine's tracked files differ from the repo. Read-only; never writes.

    Values are hidden by default: the report names the file and the changed key paths. `--values`
    adds a unified diff of the plaintext, which is the explicit opt-in to see the settings.
    """
    from syncintosh.adapters.jsonc import JsoncAdapter
    from syncintosh.crypto.cipher import EnvelopeCipher
    from syncintosh.presets import BuiltinCatalog
    from syncintosh.system.config import load_config
    from syncintosh.system.deny_list import SystemDenyList
    from syncintosh.system.live_files import SystemLiveFiles
    from syncintosh.system.paths import MachinePaths
    from syncintosh.vcs.git import GitCLITransport

    paths = MachinePaths.default()
    try:
        config = load_config(paths.config_file)
        transport = GitCLITransport(paths.repo_dir)
        # Only the read-only ports: no scanner, so a `diff` on a fresh machine does not materialize
        # the scan allowlist (`load_scan_allow` writes one when it is absent), and no lock.
        report = diff_repo_service(
            transport=transport,
            cipher=EnvelopeCipher(_key_store(config.remote)),
            live=SystemLiveFiles(),
            config=config,
            home=str(paths.home),
            deny=SystemDenyList(),
            branch=transport.branch() or BRANCH,
            catalog=BuiltinCatalog.default(),
            adapters={"jsonc": JsoncAdapter()},
            validators=validators(),
        )
    except DomainError as exc:
        _fail(exc)
        return
    _render_diff(report, values=values)


def _when_tag() -> WhenTag | None:
    """This machine's keyed tag for a `when` clause, or `None` when no key exists yet (it is never created here)."""
    from syncintosh.system.paths import MachinePaths
    from syncintosh.system.scan_allow import keyed_when_tag

    return keyed_when_tag(MachinePaths.default().scan_allow_file)


def _render_diff(report, *, values: bool) -> None:
    """Render a `DiffReport`; nothing here decides *what* differs (``AGENTS.md`` §2.3)."""
    import difflib

    if not report.entries:
        _say("nothing is tracked here yet")
        return
    _say(report.source)
    for entry in report.entries:
        keys = ", ".join(path_label(path, element, when_tag=_when_tag()) for path, element in entry.addresses)
        suffix = f" [{keys}]" if keys else ""
        _say(f"{entry.status}: {entry.path}{suffix}")
    if not values:
        return
    for entry in report.entries:
        if entry.status != "changed" or entry.here is None or entry.repo is None:
            continue
        for line in difflib.unified_diff(
            entry.repo.splitlines(),
            entry.here.splitlines(),
            fromfile=f"{entry.path} (repo)",
            tofile=f"{entry.path} (here)",
            lineterm="",
        ):
            _say(line)


@app.command()
def merge(
    base: Path,
    local: Path,
    remote: Path,
    path: str | None = typer.Option(
        None, "--path", help="the record path of the preset file, so its array rules (per binding) apply"
    ),
) -> None:
    """Three-way merge of three JSONC files, preserving comments. Exits 1 on conflicts.

    The merged tree is rendered against the local file, so comments, a trailing comma, and the
    key order survive and an unchanged element is left as it was. `--path` names the preset file the
    three come from; its arrays (`keybindings.json`) then merge one binding at a time. The rules come
    from the built-in catalog, never from the files. The adapter stays a lazy import so CLI startup
    does not pay for JSONC parsing (``AGENTS.md`` §4).
    """
    from syncintosh.adapters.jsonc import JsoncAdapter
    from syncintosh.core.errors import ContentParseError
    from syncintosh.presets import BuiltinCatalog
    from syncintosh.services import merge_text

    try:
        base_text = base.read_text(encoding="utf-8")
        local_text = local.read_text(encoding="utf-8")
        remote_text = remote.read_text(encoding="utf-8")
        result = merge_text(
            base_text,
            local_text,
            remote_text,
            JsoncAdapter(),
            catalog=BuiltinCatalog.default(),
            path=path,
        )
    except (OSError, UnicodeDecodeError, ContentParseError) as exc:
        raise typer.BadParameter(f"cannot read JSONC from {base} / {local} / {remote}: {exc}") from exc
    typer.echo(result.text, nl=False)
    if result.has_conflicts:
        for conflict in result.conflicts:
            # The same label the pending-conflict detail uses; an array at the root —
            # `keybindings.json` — is compared whole, so the document itself is the conflict.
            typer.echo(f"conflict: {path_label(conflict.path, conflict.element, when_tag=_when_tag())}", err=True)
        if result.reason:
            # The render refused: say why, so a person knows what would have to change to merge this file.
            typer.echo(f"not merged: {result.reason}", err=True)
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


@app.command()
def resolve(
    take: str | None = typer.Option(None, "--take", help="local | remote: settle every pending conflict to that side"),
    local: list[str] | None = typer.Option(
        None, "--local", help="settle the conflict on this path to local (repeatable)"
    ),
    remote: list[str] | None = typer.Option(
        None, "--remote", help="settle the conflict on this path to remote (repeatable)"
    ),
    yes: bool = typer.Option(False, "--yes", help="publish without the approval prompt"),
    llm: bool = typer.Option(False, "--llm", help="ask the configured model for each content conflict"),
) -> None:
    """Settle the conflicts a stopped sync left pending, then sync. Exits 1 when conflicts remain.

    With no side named, each conflict is shown with its values and you choose for it. With --llm the
    configured model suggests a choice for each content conflict; you accept or decline each one, and
    the final approval still applies. A run that leaves any conflict without a choice publishes nothing.
    """
    from syncintosh.services.resolve import decision, scoped_chooser, take_chooser
    from syncintosh.system.config import load_config
    from syncintosh.system.paths import MachinePaths

    scoped = bool(local or remote)
    if take is not None and take not in ("local", "remote"):
        raise typer.BadParameter("--take must be `local` or `remote`")
    if take is not None and scoped:
        raise typer.BadParameter("--take settles every conflict; drop --local/--remote, or drop --take")
    if llm and (take is not None or scoped):
        raise typer.BadParameter("--llm chooses for you; drop --take/--local/--remote to use it")
    machine = MachinePaths.default()
    approve = None if yes else _confirm_resolution
    try:
        config = load_config(machine.config_file)
        if llm and yes and config.llm.auto_apply:
            # --yes skips the approval screen, the only place an auto-applied proposal is seen.
            raise typer.BadParameter("--yes would publish auto-applied proposals unseen; drop --yes or llm.auto_apply")
        ports = _sync_ports(config, machine)
        if take is not None:
            choose = take_chooser(take)
        elif scoped:
            choose = scoped_chooser(tuple(local or ()), tuple(remote or ()), home=str(machine.home))
        elif llm:
            choose = _assisted_choice(config, machine, ports)
        else:
            choose = _ask_choice
        report = sync_service(**ports, resolving=True, decide=decision(choose, approve=approve))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except DomainError as exc:
        _fail(exc)
        return
    _render(report, config)
    if report.has_conflicts or report.needs_adopt:
        typer.echo("nothing was published; the conflicts that remain are still pending", err=True)
        raise typer.Exit(code=1)


def _assisted_choice(config, machine, ports: dict) -> Callable[[ConflictView], Choice | None]:
    """The chooser for `--llm`: the configured model first, the person by hand whenever it has no usable answer.

    The endpoint is confirmed before the first call when it is not a loopback or private-network address,
    because the conflicting values leave the machine. Nothing is sent before that answer.
    """
    from getpass import getuser

    from syncintosh.core.advice import is_public_endpoint
    from syncintosh.llm.client import OpenAIAdvisor, OpenAIChatTransport
    from syncintosh.llm.keys import llm_api_key
    from syncintosh.services.resolve import advised_chooser, auto_apply_for, proposal_problem, shell_presets
    from syncintosh.system.masking import TextMasker

    llm = config.llm
    if llm.base_url is None or llm.model is None:
        raise typer.BadParameter("set llm.base_url and llm.model in config.json before using --llm")
    if urlsplit(llm.base_url).scheme == "http" and is_public_endpoint(llm.base_url, llm.internal_domains):
        # The key and the conflicting values would travel in clear to a host off the network.
        raise typer.BadParameter("llm.base_url over plain http may not name a public host; use https")
    shell = shell_presets(ports["catalog"], llm.auto_apply)
    if shell:
        # A shell file is code the next shell runs. A model's change to it is always shown, never applied unasked.
        raise typer.BadParameter(f"llm.auto_apply names shell presets, which are always asked: {', '.join(shell)}")
    if is_public_endpoint(llm.base_url, llm.internal_domains) and not typer.confirm(
        f"{llm.base_url} is not a loopback or private-network address: the conflicting values would leave "
        "this network. Continue?",
        default=False,
    ):
        raise typer.Exit(code=1)
    catalog, adapters, validators = ports["catalog"], ports["adapters"], ports["validators"]
    return advised_chooser(
        advisor=OpenAIAdvisor(
            OpenAIChatTransport(base_url=llm.base_url, model=llm.model, api_key=llm_api_key(), timeout=60.0)
        ),
        masker=TextMasker(home=str(machine.home), username=getuser(), internal_domains=llm.internal_domains),
        fallback=_ask_choice,
        present=_present_suggestion,
        auto_apply=auto_apply_for(catalog, frozenset(llm.auto_apply)),
        check=lambda view, text: proposal_problem(
            view, text, catalog=catalog, adapters=adapters, validators=validators
        ),
    )


def _present_suggestion(suggestion: Suggestion) -> bool:
    """Show a model's suggestion with its rationale, the change it would make, and any new URL; read the accept.

    A refused suggestion is shown with the reason and declined without asking. A URL the proposal
    introduces is flagged as untrusted with its domain, whatever it matches.
    """
    from difflib import unified_diff

    from rich.markup import escape

    from syncintosh.core.resolution import Proposal

    view = suggestion.view
    console = Console()
    heading = f"[bold]{escape(view.path)}[/]  {view.kind}"
    if view.key_path is not None:
        heading += f"  key {escape(path_label(view.key_path, view.element, when_tag=_when_tag()))}"
    console.print(heading)
    console.print(f"  model: {escape(suggestion.rationale)}")
    if suggestion.refused is not None:
        console.print(f"  [red]not offered:[/] {escape(suggestion.refused)}")
        return False
    for url in suggestion.urls:
        console.print(f"  [bold yellow]UNTRUSTED {escape(url.domain)}[/]  {escape(url.url)}")
    choice = suggestion.choice
    proposed = (
        choice.text if isinstance(choice, Proposal) else (view.remote_text if choice == "remote" else view.local_text)
    )
    from syncintosh.core.advice import visible

    for line in unified_diff(view.local_text.splitlines(), proposed.splitlines(), "local", "suggested", lineterm=""):
        console.print(escape(visible(line)))
    return typer.confirm("  accept this suggestion?", default=False)


def _ask_choice(view: ConflictView) -> Choice:
    """Show one conflict with its values and read the operator's choice for it.

    Values are shown by default: resolving a conflict means comparing the two sides, and the values
    are what the person compares. Nothing shown here is logged or written to disk.
    """
    from syncintosh.services.resolve import options_for

    _show_conflict(Console(), view)
    options = options_for(view)
    by_name: dict[str, NamedChoice] = {option: option for option in options}
    answer = typer.prompt(f"  choose {' / '.join(options)}")
    while answer not in by_name:
        answer = typer.prompt(f"  not one of {' / '.join(options)}; choose again")
    return by_name[answer]


def _show_conflict(console: Console, view: ConflictView) -> None:
    """Print one conflict's place and its values. Every value is escaped, so file text is never markup."""
    from rich.markup import escape

    heading = f"[bold]{escape(view.path)}[/]  {view.kind}"
    if view.key_path is not None:
        heading += f"  key {escape(path_label(view.key_path, view.element, when_tag=_when_tag()))}"
    console.print(heading)
    if view.kind == "key":
        console.print(f"  base    {escape(view.base_text)}")
        console.print(f"  local   {escape(view.local_text)}")
        console.print(f"  remote  {escape(view.remote_text)}")
    elif view.kind == "file":
        console.print(escape(view.diff) or "  (no textual difference)")
    else:
        if view.base_text:
            console.print(f"  base    {escape(view.base_text)}")
        console.print(f"  local   {escape(view.local_text) or 'no record'}")
        console.print(f"  remote  {escape(view.remote_text) or 'no record'}")
        if view.diff:
            console.print(f"  {escape(view.diff)}")


def _confirm_resolution(views: tuple[ConflictView, ...], picks: Picks) -> bool:
    """Show what will be published, in full, and ask before anything is written to the remote.

    A value a model proposed is shown as its text, never as the repr of its carrier, and every control
    character is escaped, so the person reads what will be applied, including an auto-applied proposal.
    """
    for view in views:
        if view.fingerprint in picks:
            for line in _pick_lines(view, picks[view.fingerprint]):
                typer.echo(line)
    return typer.confirm("Publish these choices?", default=False)


def _pick_lines(view: ConflictView, pick: Choice) -> list[str]:
    """One pick as the lines a person reads before publishing: its place, then the change it makes."""
    from difflib import unified_diff

    from syncintosh.core.advice import visible
    from syncintosh.core.resolution import Proposal

    place = f"  {view.path}  {view.kind}"
    if view.key_path is not None:
        place += f"  key {path_label(view.key_path, view.element, when_tag=_when_tag())}"
    if not isinstance(pick, Proposal):
        return [f"{place}  ->  {pick}"]
    if view.kind == "file":
        change = unified_diff(view.local_text.splitlines(), pick.text.splitlines(), "local", "proposed", lineterm="")
        return [f"{place}  ->  proposed by the model", *(f"    {visible(line)}" for line in change)]
    return [f"{place}  ->  proposed by the model", f"    {visible(pick.text)}"]


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
    from syncintosh.system.scan_allow import keyed_when_tag, load_scan_allow
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
        "when_tag": keyed_when_tag(paths.scan_allow_file),
    }


@app.command()
def watch(
    quiet: float = typer.Option(
        DEFAULT_QUIET_SECONDS, "--quiet", help="seconds of quiet before a change triggers a sync"
    ),
    poll: float | None = typer.Option(
        None,
        "--poll",
        min=1,
        help="seconds between remote-polling syncs (the daemon sets this); omit for event-driven only",
    ),
) -> None:
    """Watch the tracked paths and sync after a change settles. Runs in the foreground.

    `syncintosh daemon install` runs this under launchd with `--poll`, so a change on another Mac
    also arrives without a local edit.
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
            poll_seconds=poll,
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
        _say("these files exist here and in the repo with different content:")
        for path in report.first_sight:
            _say(f"  {path}")
        _say("re-run with `sync --adopt local` (keep these) or `sync --adopt remote` (take the repo's)")
    if report.conflicts:
        _say("conflicts stopped the sync; nothing was applied:", err=True)
        for conflict in report.conflicts:
            _say(f"  {conflict.path or conflict.file_id}: {conflict.detail}", err=True)
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
            _say(f"{label}: {path}")
    for deferral in report.deferred:
        # A deferral is not a failure (the next sync on a machine that has the app writes it), so it
        # goes to stdout, with the app's own download page when that is why it was deferred.
        _say(f"deferred ({deferral.reason}): {deferral.path}")
        if deferral.download_url:
            _say(f"  download: {deferral.download_url}")
        if deferral.cask:
            _say(f"  or: brew install --cask {deferral.cask}")
    for refusal in report.refused:
        if refusal.blocking:
            # The reason is a fixed phrase, never file content (``AGENTS.md`` §4).
            _say(f"refused ({refusal.reason}): {refusal.path}", err=True)
        else:
            # Not `skipped`: that word already means a dangerous path here, and the two call for very
            # different reactions. The reason is shown too, so "the glob is full" is not mistaken for
            # "this machine does not track the path".
            _say(f"not synced ({refusal.reason}): {refusal.path}")
    for label, refusals in (("unprojectable", report.unprojectable), ("invalid", report.invalid)):
        for refusal in refusals:
            _say(f"{label} ({refusal.reason}): {refusal.path}", err=True)
    if report.unchanged:
        _say("nothing changed")
    elif report.published_commit:
        _say(f"published {report.published_commit[:12]}")
    if report.applied and report.backup_commit:
        _say(f"applied from {report.backup_commit[:12]}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
