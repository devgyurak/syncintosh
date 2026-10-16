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
from collections.abc import Sequence
from pathlib import Path

from syncintosh.core.errors import GitError
from syncintosh.core.ports import REMOTE_REF_PREFIX, PushOutcome

# The commits syncintosh writes are machine-generated and deterministic; a fixed identity keeps
# them reproducible and keeps a test off the developer's real git config.
COMMIT_NAME = "syncintosh"
COMMIT_EMAIL = "syncintosh@localhost"

# git echoes the remote URL in its errors, and that URL can carry a credential
# (`https://user:token@host`). Redact the userinfo before a message can reach a log
# (``AGENTS.md`` §4).
_CREDENTIALS_RE = re.compile(r"://[^/@\s]*@")
_MAX_DETAIL = 400


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
