# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `GitTransport` port over the real `git` CLI.

Only the plumbing the sync cycle needs, and **never** `pull`, `merge`, or `rebase`: sync
decrypts both sides, merges the plaintext, and builds the resulting tree with `write-tree` +
`commit-tree` (``docs/design/01-sync-model.md``). `fetch` and `clone` never pass `--depth`,
because `merge-base` needs history.

Two deliberate choices:

* **No shell.** Every command is an argv list, so a path or a remote URL can never be word-split
  or interpolated by a shell.
* **A fixed environment.** The author/committer identity and `LC_ALL` are pinned so commits are
  deterministic and message parsing is stable, independent of the user's git config. The ambient
  environment is kept, so a credential helper or an SSH agent still works. A failure message
  names only the git subcommand, never its arguments — a remote URL can carry a token
  (``AGENTS.md`` §4).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO

from syncintosh.core.envelope_header import HEADER_SIZE, header_generation
from syncintosh.core.errors import GitError
from syncintosh.core.ports import REMOTE_REF_PREFIX, PushOutcome
from syncintosh.core.repo import MANIFEST_PATH, object_file_id

# The commits syncintosh writes are machine-generated and deterministic; a fixed identity keeps
# them reproducible and keeps a test off the developer's real git config.
COMMIT_NAME = "syncintosh"
COMMIT_EMAIL = "syncintosh@localhost"

# git echoes the remote URL in its errors, and that URL can carry a credential
# (`https://user:token@host`). Redact the userinfo before a message can reach a log
# (``AGENTS.md`` §4).
_CREDENTIALS_RE = re.compile(r"://[^/@\s]*@")
_MAX_DETAIL = 400
# A blob's body is skipped in bounded chunks so a large object never sits in memory whole.
_DISCARD_CHUNK = 64 * 1024


def _redact(text: str) -> str:
    """Strip URL credentials and bound the length of a git error detail."""
    return _CREDENTIALS_RE.sub("://<redacted>@", text.strip())[:_MAX_DETAIL]


class GitCLITransport:
    """A `GitTransport` that shells out to `git` with a fixed environment."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    # -- process plumbing -------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_NAME": COMMIT_NAME,
                "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
                "GIT_COMMITTER_NAME": COMMIT_NAME,
                "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )
        return env

    def _run(self, args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
        argv = ["git", "-c", "commit.gpgsign=false", *args]
        return subprocess.run(argv, cwd=str(cwd), env=self._env(), capture_output=True, check=False)

    def _checked(self, args: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        result = self._run(args, cwd=self._workdir if cwd is None else cwd)
        if result.returncode != 0:
            # The subcommand plus a redacted detail. A remote URL can carry a credential, and
            # git echoes the URL, so the userinfo is stripped (``AGENTS.md`` §4).
            detail = _redact(result.stderr.decode("utf-8", "replace"))
            raise GitError(f"`git {args[0]}` failed (exit {result.returncode}): {detail}")
        return result

    # -- the port ---------------------------------------------------------

    def init(self) -> None:
        """Create the repository in the working tree, on the `main` branch."""
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._checked(["init", "-b", "main"])

    def clone(self, remote: str) -> None:
        """Clone `remote` with full history (never `--depth`), on a branch that exists."""
        self._workdir.parent.mkdir(parents=True, exist_ok=True)
        self._checked(["clone", "--quiet", remote, str(self._workdir)], cwd=self._workdir.parent)
        self._adopt_default_branch()

    def _adopt_default_branch(self) -> None:
        """Point HEAD at a real branch when the remote's HEAD named one it does not have.

        A bare remote made by a plain `git init --bare` points HEAD at `master` while the first
        publisher may have pushed `main`; `git clone` then leaves HEAD unborn, so nothing
        resolves, and every sync would commit to a branch no other machine reads. With exactly
        one branch the choice is unambiguous; anything else is refused rather than left unborn,
        because an unborn HEAD silently discards the cycle's commits.
        """
        if self.head() is not None:
            return
        result = self._checked(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"])
        names = result.stdout.decode("ascii").split()
        if len(names) != 1:
            raise GitError(
                f"the remote's HEAD names a branch it does not have, and origin has {names}; "
                "set the remote's default branch"
            )
        branch = names[0].removeprefix("origin/")
        self._checked(["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
        self._checked(["update-ref", f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"])
        self._checked(["read-tree", "--reset", "-u", f"refs/heads/{branch}"])

    def fetch(self, remote: str, branch: str) -> None:
        """Fetch every branch from `remote` into the local tracking namespace.

        A wildcard refspec, not `refs/heads/<branch>` alone: fetching one branch fails on a repo
        with no commits yet, and the first push is into exactly such a repo.
        """
        self._checked(["fetch", "--quiet", remote, f"+refs/heads/*:{REMOTE_REF_PREFIX}*"])

    def head(self) -> str | None:
        """The current commit id, or `None` on an unborn branch."""
        return self._rev_parse("HEAD")

    def branch(self) -> str | None:
        """The branch HEAD points at, or `None` when it is detached."""
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=self._workdir)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii").strip()

    def resolve(self, rev: str) -> str | None:
        """The commit id `rev` names, or `None` when it does not exist."""
        return self._rev_parse(rev)

    def _rev_parse(self, rev: str) -> str | None:
        result = self._run(["rev-parse", "--verify", "--quiet", rev], cwd=self._workdir)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii").strip()

    def merge_bases(self, left: str, right: str) -> tuple[str, ...]:
        """Every merge base of `left` and `right`, sorted; empty when unrelated."""
        result = self._run(["merge-base", "--all", left, right], cwd=self._workdir)
        if result.returncode not in (0, 1):
            raise GitError(f"`git merge-base` failed (exit {result.returncode})")
        return tuple(sorted(result.stdout.decode("ascii").split()))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True when `ancestor` is reachable from `descendant`."""
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant], cwd=self._workdir)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitError(f"`git merge-base --is-ancestor` failed (exit {result.returncode})")

    def read_blob(self, commit: str, relpath: str) -> bytes | None:
        """The blob at `relpath` in `commit`, or `None` when it is absent."""
        result = self._run(["cat-file", "blob", f"{commit}:{relpath}"], cwd=self._workdir)
        if result.returncode != 0:
            return None
        return result.stdout

    def commit(self, parents: tuple[str, ...], message: str, paths: tuple[str, ...]) -> str:
        """Stage `paths` (additions, changes, and deletions) and create a commit with `parents`.

        The pathspec is explicit, not `add -A .`: `read-tree --reset` cannot remove an untracked
        file, so a stray one would otherwise be committed. A path is included when the working
        tree has it **or** the index does — a file deleted inside the pathspec must still be
        staged as a deletion — and `git add` rejects a pathspec that matches nothing, so a
        directory that does not exist yet is skipped.
        """
        stageable = [path for path in paths if self._stageable(path)]
        if stageable:
            self._checked(["add", "-A", "--", *stageable])
        tree = self._checked(["write-tree"]).stdout.decode("ascii").strip()
        parent_args = [token for parent in parents for token in ("-p", parent)]
        return self._checked(["commit-tree", tree, *parent_args, "-m", message]).stdout.decode("ascii").strip()

    def _stageable(self, path: str) -> bool:
        """True when `path` exists in the working tree or is known to the index."""
        if (self._workdir / path).exists():
            return True
        result = self._run(["ls-files", "--", path], cwd=self._workdir)
        return result.returncode == 0 and bool(result.stdout.strip())

    def update_ref(self, branch: str, commit: str) -> None:
        """Point `refs/heads/<branch>` at `commit` without touching the working tree."""
        self._checked(["update-ref", f"refs/heads/{branch}", commit])

    def materialize(self, commit: str) -> None:
        """Reset the index and working tree to `commit`'s tree (`read-tree --reset`).

        Not a merge: the tree is checked out wholesale, so a fast-forward can move the branch
        forward and leave the working tree a later `add -A` will stage correctly.
        """
        self._checked(["read-tree", "--reset", "-u", commit])

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        """Push `commit` to `branch` on `remote`, classified for the retry rule."""
        result = self._run(["push", "--porcelain", remote, f"{commit}:refs/heads/{branch}"], cwd=self._workdir)
        if result.returncode == 0:
            return "ok"
        combined = (result.stdout + result.stderr).decode("utf-8", "replace").lower()
        if "non-fast-forward" in combined or "fetch first" in combined or "stale info" in combined:
            return "non-fast-forward"
        return "rejected"

    def generations_in_reachable_objects(self, tips: tuple[str, ...]) -> tuple[int, ...]:
        """The generations in every stored envelope reachable from `tips` (``rev-list`` + `cat-file`).

        Only the cleartext key-generation header is read, so no key is needed. `rev-list --objects`
        lists each reachable blob once with the path it is first found under; the envelope slots are
        filtered out, and each blob is then asked of one `cat-file --batch` whose answer is read
        5 bytes deep and skipped, so memory stays flat however large the history is.
        """
        if not tips:
            return ()
        oids: dict[str, None] = {}
        with self._spawn(["rev-list", "--objects", *tips], feed=False) as listing:
            assert listing.stdout is not None
            for raw in listing.stdout:
                oid, separator, path = raw.decode("ascii", "replace").rstrip("\n").partition(" ")
                # The repo's own slot recogniser decides what is an envelope: `objects/<id>.enc` or
                # `manifest.enc`. `object_file_id` full-matches the slot, so a nested or unrelated
                # path never counts (``core/repo.py``).
                if separator and (path == MANIFEST_PATH or object_file_id(path) is not None):
                    oids[oid] = None
        if not oids:
            return ()
        with self._spawn(["cat-file", "--batch"], feed=True) as batch:
            assert batch.stdin is not None and batch.stdout is not None
            try:
                return _batch_generations(batch.stdin, batch.stdout, oids)
            except BrokenPipeError as exc:
                # git died mid-run; `_spawn` swaps in its exit code and stderr.
                raise GitError("`git cat-file --batch` closed its input early") from exc

    def remote_reachable(self, remote: str) -> bool:
        """True when `remote` answers a read-only `git ls-remote`, without fetching objects."""
        result = self._run(["ls-remote", "--quiet", remote], cwd=self._workdir)
        return result.returncode == 0

    @contextmanager
    def _spawn(self, args: Sequence[str], *, feed: bool) -> Generator[subprocess.Popen[bytes]]:
        """Run a git subcommand whose output is read incrementally, and always reap the child.

        stderr goes to a temporary file rather than a pipe: nothing reads it while stdout is being
        streamed, so a pipe could fill and stall git. Without `--buffer`, `cat-file --batch` flushes
        after each object, so one request then one response needs no reader thread to stay live.
        """
        argv = ["git", "-c", "commit.gpgsign=false", *args]
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                argv,
                cwd=str(self._workdir),
                env=self._env(),
                stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            try:
                try:
                    yield proc
                except GitError as exc:
                    # The block gave up on git's output (cut off, malformed, or its input closed). If git
                    # itself failed, its exit code and stderr are the real cause, so surface them.
                    detailed = self._with_exit_detail(exc, proc, errors, args[0])
                    if detailed is exc:
                        raise  # nothing more to say; chaining an error to itself would make a self-referential cause
                    raise detailed from exc
                if proc.stdin is not None:
                    proc.stdin.close()
                assert proc.stdout is not None
                proc.stdout.read()  # let git finish writing so `wait` cannot block on a full pipe
                code = proc.wait()
                if code != 0:
                    raise GitError(f"`git {args[0]}` failed (exit {code}): {self._stderr_detail(errors)}")
            finally:
                if proc.poll() is None:
                    proc.kill()
                for pipe in (proc.stdin, proc.stdout):
                    if pipe is not None:
                        # A pipe to a dead child can raise again on close (it flushes buffered bytes);
                        # that must not stop the child from being reaped below.
                        with suppress(OSError):
                            pipe.close()
                proc.wait()

    @staticmethod
    def _stderr_detail(errors: IO[bytes]) -> str:
        errors.seek(0)
        return _redact(errors.read().decode("utf-8", "replace"))

    def _with_exit_detail(
        self, failure: GitError, proc: subprocess.Popen[bytes], errors: IO[bytes], subcommand: str
    ) -> GitError:
        """`failure`, or a `GitError` naming git's own exit code and stderr when git already died."""
        try:
            code = proc.wait(timeout=1)  # a dying child may not be reapable the instant its pipe breaks
        except subprocess.TimeoutExpired:
            return failure  # git is alive, so the stream really was malformed
        if code == 0:
            return failure
        return GitError(f"`git {subcommand}` failed (exit {code}): {self._stderr_detail(errors)}")


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    """Exactly `size` bytes of `stream`; a short read means git's output was cut off."""
    data = stream.read(size)
    if len(data) != size:
        raise GitError("`git cat-file --batch` ended before the object did")
    return data


def _discard(stream: IO[bytes], size: int) -> None:
    """Skip `size` bytes of `stream` in bounded chunks."""
    while size > 0:
        chunk = stream.read(min(size, _DISCARD_CHUNK))
        if not chunk:
            raise GitError("`git cat-file --batch` ended before the object did")
        size -= len(chunk)


def _batch_generations(stdin: IO[bytes], stdout: IO[bytes], oids: Iterable[str]) -> tuple[int, ...]:
    """The distinct, positive key generations named in the first header of each object in `oids`.

    Speaks the `cat-file --batch` protocol one object at a time: write an oid, read its
    `<oid> SP <type> SP <size> LF` line, keep the first `HEADER_SIZE` bytes of the body, and skip the
    rest plus its trailing LF. `<oid> SP missing LF` contributes nothing. The header parse has one
    owner — `core.envelope_header.header_generation` — so a format change cannot silently make this
    walk miss a generation and let `key prune` drop a needed key.
    """
    generations: set[int] = set()
    for oid in oids:
        stdin.write(oid.encode("ascii") + b"\n")
        stdin.flush()
        fields = stdout.readline().split()
        if len(fields) == 2 and fields[1] == b"missing":
            continue
        try:
            if len(fields) != 3 or fields[0] != oid.encode("ascii"):
                raise ValueError(fields)
            size = int(fields[2])
        except ValueError as exc:
            # A malformed stream is a git failure, not a caller error; keep it a `DomainError`.
            raise GitError("could not parse `git cat-file --batch` output") from exc
        head = _read_exact(stdout, min(size, HEADER_SIZE))
        _discard(stdout, size - len(head) + 1)  # the rest of the body, then its trailing LF
        generation = header_generation(head)
        if generation is not None:
            generations.add(generation)
    return tuple(sorted(generations))
