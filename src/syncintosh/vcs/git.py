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
import subprocess
from collections.abc import Sequence
from pathlib import Path

from syncintosh.core.errors import GitError
from syncintosh.core.ports import PushOutcome

# The commits syncintosh writes are machine-generated and deterministic; a fixed identity keeps
# them reproducible and keeps a test off the developer's real git config.
COMMIT_NAME = "syncintosh"
COMMIT_EMAIL = "syncintosh@localhost"

# A single namespace for the fetched remote branch. The remote is a URL from config, not a
# configured `origin`, so the tracking ref is named explicitly rather than inferred.
REMOTE_REF_PREFIX = "refs/remotes/syncintosh/"


def remote_ref(branch: str) -> str:
    """The local tracking ref a fetch of `branch` writes (``syncintosh/<branch>``)."""
    return f"{REMOTE_REF_PREFIX}{branch}"


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
            # Only the subcommand, never the arguments: a remote URL can carry a token.
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"`git {args[0]}` failed (exit {result.returncode}): {detail}")
        return result

    # -- the port ---------------------------------------------------------

    def init(self) -> None:
        """Create the repository in the working tree, on the `main` branch."""
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._checked(["init", "-b", "main"])

    def clone(self, remote: str) -> None:
        """Clone `remote` with full history (never `--depth`)."""
        self._workdir.parent.mkdir(parents=True, exist_ok=True)
        self._checked(["clone", "--quiet", remote, str(self._workdir)], cwd=self._workdir.parent)

    def fetch(self, remote: str, branch: str) -> None:
        """Fetch `branch` into the local tracking ref for `branch`."""
        self._checked(["fetch", "--quiet", remote, f"+refs/heads/{branch}:{remote_ref(branch)}"])

    def head(self) -> str | None:
        """The current commit id, or `None` on an unborn branch."""
        return self._rev_parse("HEAD")

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

    def commit(self, parents: tuple[str, ...], message: str) -> str:
        """Stage the working tree as-is and create a commit with `parents`."""
        self._checked(["add", "-A"])
        tree = self._checked(["write-tree"]).stdout.decode("ascii").strip()
        parent_args = [token for parent in parents for token in ("-p", parent)]
        return self._checked(["commit-tree", tree, *parent_args, "-m", message]).stdout.decode("ascii").strip()

    def update_ref(self, branch: str, commit: str) -> None:
        """Point `refs/heads/<branch>` at `commit` without touching the working tree."""
        self._checked(["update-ref", f"refs/heads/{branch}", commit])

    def push(self, remote: str, branch: str, commit: str) -> PushOutcome:
        """Push `commit` to `branch` on `remote`, classified for the retry rule."""
        result = self._run(["push", "--porcelain", remote, f"{commit}:refs/heads/{branch}"], cwd=self._workdir)
        if result.returncode == 0:
            return "ok"
        combined = (result.stdout + result.stderr).decode("utf-8", "replace").lower()
        if "non-fast-forward" in combined or "fetch first" in combined or "stale info" in combined:
            return "non-fast-forward"
        return "rejected"
