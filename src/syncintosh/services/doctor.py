# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: check that this machine's install can actually sync.

`doctor` gathers the things a silent failure hides: a launchd plist that runs a stale entry point,
a generation the repo needs but this machine does not hold, a remote that does not answer, a lock
another sync holds, and an unresolved conflict. Every check **reports** rather than raises, so one
broken thing does not hide the rest (``docs/design/README.md``, launchd).
"""

from __future__ import annotations

from dataclasses import dataclass

from syncintosh.core.errors import DomainError, SyncLockError
from syncintosh.core.ports import GitTransport, KeyStore, PendingStore, SyncLock, remote_ref


@dataclass(frozen=True)
class Check:
    """One diagnostic, with the sentence a user reads."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    """Every check, in a stable order."""

    checks: tuple[Check, ...]

    @property
    def healthy(self) -> bool:
        """True when every check passed."""
        return all(check.ok for check in self.checks)


def doctor(
    *,
    transport: GitTransport,
    store: KeyStore,
    lock: SyncLock,
    pending: PendingStore,
    remote: str,
    branch: str,
    plist_entry_point: str | None,
    expected_entry_point: str | None,
    job_loaded: bool | None = None,
    extra: tuple[Check, ...] = (),
) -> DoctorReport:
    """Run every check and return the report. A check never raises; it reports its failure."""
    return DoctorReport(
        checks=(
            _check_daemon(plist_entry_point, expected_entry_point, job_loaded),
            _check_pending(pending),
            _check_lock(lock),
            _check_remote(transport, remote),
            _check_generations(transport, store, branch),
            *extra,
        )
    )


def _check_daemon(plist_entry_point: str | None, expected_entry_point: str | None, job_loaded: bool | None) -> Check:
    """The installed plist must run this build's entry point and be loaded by launchd."""
    if plist_entry_point is None:
        return Check("daemon", True, "not installed")
    if expected_entry_point is not None and plist_entry_point != expected_entry_point:
        return Check(
            "daemon",
            False,
            f"the plist runs {plist_entry_point} but this build is {expected_entry_point}; "
            "reinstall with `syncintosh daemon install`",
        )
    if job_loaded is False:
        return Check(
            "daemon",
            False,
            "the plist is installed but launchd has not loaded the job; reinstall with `syncintosh daemon install`",
        )
    return Check("daemon", True, f"runs {plist_entry_point}")


def _check_pending(pending: PendingStore) -> Check:
    try:
        is_pending = pending.is_pending()
    except DomainError as exc:
        return Check("pending", False, str(exc))
    if is_pending:
        return Check("pending", False, "a sync conflict is unresolved; run `syncintosh resolve`")
    return Check("pending", True, "no unresolved conflict")


def _check_lock(lock: SyncLock) -> Check:
    try:
        lock.acquire()
    except SyncLockError:
        return Check("lock", False, "another sync is running; wait for it")
    lock.release()
    return Check("lock", True, "the lock is free")


def _check_remote(transport: GitTransport, remote: str) -> Check:
    try:
        reachable = transport.remote_reachable(remote)
    except DomainError as exc:
        return Check("remote", False, str(exc))
    if reachable:
        return Check("remote", True, "reachable")
    return Check("remote", False, "the remote did not answer")


def _check_generations(transport: GitTransport, store: KeyStore, branch: str) -> Check:
    """Every generation reachable history uses must be held, or a merge-base cannot be decrypted.

    Read-only: it walks this machine's `HEAD` and its **local** tracking ref for the remote, never
    fetching. A diagnostic must not move refs (``GitTransport.remote_reachable``), and a stale
    tracking ref only makes the check report less, never a false failure.
    """
    try:
        tips = tuple(tip for tip in (transport.head(), transport.resolve(remote_ref(branch))) if tip is not None)
        needed = transport.generations_in_reachable_objects(tips)
        missing = [generation for generation in needed if store.get(generation) is None]
    except DomainError as exc:
        return Check("generations", False, f"could not check the repo: {exc}")
    if missing:
        return Check("generations", False, f"this machine does not hold {missing}; run `syncintosh key import`")
    return Check("generations", True, f"holds every generation reachable history uses ({len(needed)})")


__all__ = ["Check", "DoctorReport", "doctor"]
