# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the git CLI transport over a real git binary.

It exercises the exact plumbing the sync cycle needs — a two-parent `commit-tree`, a full
(non-shallow) clone, `merge-base`, and a non-fast-forward push — against a temp bare remote, so
the transport contracts the cycle relies on are pinned with real git rather than a fake
(``AGENTS.md`` §5.1).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from syncintosh.core.errors import GitError, PruneRefused
from syncintosh.core.ports import remote_ref
from syncintosh.services.keys import prune_generation
from syncintosh.vcs.git import GitCLITransport

pytestmark = pytest.mark.integration

GIT_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")
# These transports stage a throwaway repo, so the whole tree is the pathspec.
ALL_PATHS: tuple[str, ...] = (".",)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(cwd: Path, relpath: str, data: bytes) -> None:
    path = cwd / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _settle(transport: GitCLITransport, workdir: Path, relpath: str, data: bytes, parents: tuple[str, ...]) -> str:
    """Write a file, commit it, and move `main` to the new commit."""
    _write(workdir, relpath, data)
    commit = transport.commit(parents, "commit", ALL_PATHS)
    transport.update_ref("main", commit)
    return commit


def test_a_root_commit_and_a_read_back(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    _write(workdir, "a.txt", b"hello")

    # when
    commit = transport.commit((), "root", ALL_PATHS)
    transport.update_ref("main", commit)

    # then
    assert transport.head() == commit
    assert transport.resolve("main") == commit
    assert transport.read_blob(commit, "a.txt") == b"hello"
    assert transport.read_blob(commit, "missing.txt") is None
    assert transport.resolve("does-not-exist") is None


def test_push_clone_and_fetch_round_trip(tmp_path) -> None:
    # given — machine one publishes a root commit to a bare remote
    workdir = tmp_path / "work"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True)
    transport = GitCLITransport(workdir)
    transport.init()
    first = _settle(transport, workdir, "a.txt", b"hello", ())

    # when
    pushed = transport.push(str(remote), "main", first)
    second_dir = tmp_path / "second"
    second = GitCLITransport(second_dir)
    second.clone(str(remote))

    # then
    assert pushed == "ok"
    assert second.head() == first
    assert second.read_blob(first, "a.txt") == b"hello"
    # The no-depth rule (docs/design/01-sync-model.md): a shallow clone cannot merge-base.
    assert _git(second_dir, "rev-parse", "--is-shallow-repository") == "false"


def test_fetch_updates_the_tracking_ref_and_merge_base_resolves(tmp_path) -> None:
    # given — machine two is one commit behind
    workdir = tmp_path / "work"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True)
    transport = GitCLITransport(workdir)
    transport.init()
    first = _settle(transport, workdir, "a.txt", b"hello", ())
    transport.push(str(remote), "main", first)
    second_dir = tmp_path / "second"
    second = GitCLITransport(second_dir)
    second.clone(str(remote))
    second_commit = _settle(transport, workdir, "b.txt", b"more", (first,))
    transport.push(str(remote), "main", second_commit)

    # when
    second.fetch(str(remote), "main")

    # then
    assert second.resolve(remote_ref("main")) == second_commit
    assert second.resolve("syncintosh/main") == second_commit
    assert second.merge_bases(first, "syncintosh/main") == (first,)
    assert second.is_ancestor(first, second_commit) is True
    assert second.is_ancestor(second_commit, first) is False


def test_a_divergent_push_is_reported_as_non_fast_forward(tmp_path) -> None:
    # given — a remote advanced by one branch while a clone committed its own line
    workdir = tmp_path / "work"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True)
    transport = GitCLITransport(workdir)
    transport.init()
    first = _settle(transport, workdir, "a.txt", b"hello", ())
    transport.push(str(remote), "main", first)
    second_dir = tmp_path / "second"
    second = GitCLITransport(second_dir)
    second.clone(str(remote))
    advanced = _settle(transport, workdir, "b.txt", b"more", (first,))
    transport.push(str(remote), "main", advanced)
    divergent = _settle(second, second_dir, "c.txt", b"local", (first,))

    # when
    outcome = second.push(str(remote), "main", divergent)

    # then
    assert outcome == "non-fast-forward"


def test_an_unrelated_history_has_no_merge_base(tmp_path) -> None:
    # given — two root commits with no common ancestor
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    left = transport.commit((), "root left", ALL_PATHS)
    _write(workdir, "b.txt", b"changed")
    right = transport.commit((), "root right", ALL_PATHS)

    # when
    bases = transport.merge_bases(left, right)

    # then
    assert bases == ()
    assert transport.is_ancestor(left, right) is False


def test_a_criss_cross_history_has_two_merge_bases(tmp_path) -> None:
    # given — M1 and M2 merge each other's parents, so both parents are merge bases
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    _write(workdir, "a.txt", b"x")
    root = _settle(transport, workdir, "a.txt", b"x", ())
    first = _settle(transport, workdir, "a.txt", b"left", (root,))
    second = _settle(transport, workdir, "a.txt", b"right", (root,))
    tree = _git(workdir, "rev-parse", f"{root}^{{tree}}")
    merge_one = _git(workdir, "commit-tree", tree, "-p", first, "-p", second, "-m", "M1")
    merge_two = _git(workdir, "commit-tree", tree, "-p", second, "-p", first, "-m", "M2")

    # when
    bases = transport.merge_bases(merge_one, merge_two)

    # then
    assert bases == tuple(sorted((first, second)))


def test_materialize_resets_the_working_tree_to_a_commit(tmp_path) -> None:
    # given — a repo whose second commit added a file
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    first = _settle(transport, workdir, "a.txt", b"one", ())
    _settle(transport, workdir, "b.txt", b"two", (first,))

    # when — the branch moves back to the first commit
    transport.materialize(first)
    transport.update_ref("main", first)

    # then — the working tree matches, and git reports a clean state
    assert (workdir / "a.txt").read_bytes() == b"one"
    assert (workdir / "b.txt").exists() is False
    assert _git(workdir, "status", "--porcelain") == ""


def test_a_revision_that_does_not_exist_resolves_to_none(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()

    # when & then
    assert transport.resolve("HEAD") is None


def test_branch_is_none_when_head_is_detached(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    commit = _settle(transport, workdir, "a.txt", b"x", ())
    _git(workdir, "checkout", "--detach", commit)

    # when & then
    assert transport.branch() is None


def test_commit_stages_only_the_given_paths(tmp_path) -> None:
    # given — a stray file that is not part of the repo layout
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    _write(workdir, "tracked.txt", b"one")
    _write(workdir, "stray.bin", b"junk")

    # when
    commit = transport.commit((), "root", ("tracked.txt",))
    transport.update_ref("main", commit)

    # then — the stray file never reaches the commit
    tracked = _git(workdir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert tracked == ["tracked.txt"]


def test_commit_stages_a_deletion_inside_the_pathspec(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    _write(workdir, "a.txt", b"a")
    _write(workdir, "b.txt", b"b")
    first = transport.commit((), "root", ("a.txt", "b.txt"))
    transport.update_ref("main", first)

    # when — b.txt is gone from the working tree
    (workdir / "b.txt").unlink()
    second = transport.commit((first,), "drop b", ("a.txt", "b.txt"))
    transport.update_ref("main", second)

    # then — the deletion inside the pathspec is staged
    assert transport.read_blob(second, "b.txt") is None
    assert transport.read_blob(second, "a.txt") == b"a"


def test_clone_refuses_an_ambiguous_remote_head(tmp_path) -> None:
    # given — a bare remote whose HEAD names a missing branch, holding two branches
    workdir = tmp_path / "work"
    remote = tmp_path / "legacy.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    transport = GitCLITransport(workdir)
    transport.init()
    first = _settle(transport, workdir, "a.txt", b"one", ())
    transport.push(str(remote), "main", first)
    other_dir = tmp_path / "other"
    other = GitCLITransport(other_dir)
    other.init()
    second = _settle(other, other_dir, "b.txt", b"two", ())
    other.push(str(remote), "other", second)

    # when & then — no branch is unambiguously the default, so the clone is refused rather than
    # left on an unborn HEAD that would silently discard every commit
    with pytest.raises(GitError):
        GitCLITransport(tmp_path / "third").clone(str(remote))


def test_clone_adopts_the_branch_the_remote_actually_has(tmp_path) -> None:
    # given — a bare remote whose HEAD names `master`, with only a `main` branch pushed to it
    workdir = tmp_path / "work"
    remote = tmp_path / "legacy.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    transport = GitCLITransport(workdir)
    transport.init()
    first = _settle(transport, workdir, "a.txt", b"one", ())
    assert transport.push(str(remote), "main", first) == "ok"

    # when
    second_dir = tmp_path / "second"
    second = GitCLITransport(second_dir)
    second.clone(str(remote))

    # then — HEAD is put on the branch that exists, not left unborn on `master`
    assert second.branch() == "main"
    assert second.head() == first


def test_a_bad_parent_raises_a_git_error(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    _write(workdir, "a.txt", b"x")

    # when & then
    with pytest.raises(GitError):
        transport.commit(("0" * 40,), "bad parent", ALL_PATHS)


def test_a_bad_revision_raises_a_git_error(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    commit = _settle(transport, workdir, "a.txt", b"x", ())

    # when & then — an unresolvable revision is a failure, not an empty answer
    with pytest.raises(GitError):
        transport.merge_bases("0" * 40, commit)
    with pytest.raises(GitError):
        transport.is_ancestor("0" * 40, commit)


def test_a_push_to_an_unusable_remote_is_rejected(tmp_path) -> None:
    # given
    workdir = tmp_path / "work"
    transport = GitCLITransport(workdir)
    transport.init()
    commit = _settle(transport, workdir, "a.txt", b"x", ())

    # when — the remote path does not exist, so this is neither a fast-forward nor a race
    outcome = transport.push(str(tmp_path / "no-such-remote.git"), "main", commit)

    # then
    assert outcome == "rejected"


# -- reachability read (key prune / doctor) -----------------------------------

BIG_BODY = b"x" * (300 * 1024)  # several discard chunks, so the stream is skipped rather than buffered
ENVELOPE_IDS = ("1" * 32, "2" * 32)


class _Lock:
    def acquire(self) -> None:
        return None

    def release(self) -> None:
        return None


def _envelope(generation: int, body: bytes = BIG_BODY) -> bytes:
    return bytes([1]) + generation.to_bytes(4, "big") + body


def _history_across_generations(tmp_path: Path) -> tuple[GitCLITransport, str, str]:
    """A published branch whose commits hold large envelopes under generations 2, 3, then 4."""
    workdir = tmp_path / "work"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True)
    transport = GitCLITransport(workdir)
    transport.init()
    commit = _settle(transport, workdir, "manifest.enc", _envelope(2), ())
    commit = _settle(transport, workdir, "objects/" + ENVELOPE_IDS[0] + ".enc", _envelope(3), (commit,))
    # An envelope-shaped blob outside a slot must not count, however large or well-formed.
    commit = _settle(transport, workdir, "notes/stray.enc", _envelope(9), (commit,))
    commit = _settle(transport, workdir, "manifest.enc", _envelope(4), (commit,))
    assert transport.push(str(remote), "main", commit) == "ok"
    return transport, str(remote), commit


def test_reachable_generations_include_every_commit_in_the_history(tmp_path) -> None:
    # given
    transport, _, commit = _history_across_generations(tmp_path)

    # when
    generations = transport.generations_in_reachable_objects((commit,))

    # then — gen 2 survives only in an older commit's manifest, gen 9 sits outside a slot
    assert generations == (2, 3, 4)


def test_reachable_generations_without_tips_are_empty(tmp_path) -> None:
    # given
    transport = GitCLITransport(tmp_path / "work")
    transport.init()

    # when & then
    assert transport.generations_in_reachable_objects(()) == ()


def test_prune_refuses_a_generation_a_large_old_envelope_still_uses(tmp_path, memory_key_store) -> None:
    # given
    transport, remote, _ = _history_across_generations(tmp_path)
    store = memory_key_store({1: b"k" * 32, 2: b"k" * 32, 3: b"k" * 32, 5: b"k" * 32})

    # when & then
    with pytest.raises(PruneRefused):
        prune_generation(transport=transport, store=store, lock=_Lock(), remote=remote, branch="main", generation=3)


def test_prune_drops_a_generation_no_reachable_envelope_names(tmp_path, memory_key_store) -> None:
    # given
    transport, remote, _ = _history_across_generations(tmp_path)
    store = memory_key_store({1: b"k" * 32, 4: b"k" * 32, 5: b"k" * 32})

    # when
    prune_generation(transport=transport, store=store, lock=_Lock(), remote=remote, branch="main", generation=5)

    # then
    assert store.generations() == (1, 4)


def test_a_failing_rev_list_is_a_git_error(tmp_path) -> None:
    # given — a directory that is not a repository
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir()

    # when & then
    with pytest.raises(GitError, match="rev-list"):
        GitCLITransport(workdir).generations_in_reachable_objects(("0" * 40,))


def test_a_failing_cat_file_is_a_redacted_git_error(tmp_path, monkeypatch) -> None:
    # given — a `git` that behaves normally except that `cat-file` dies with a URL-bearing message
    transport, _, commit = _history_across_generations(tmp_path)
    real_git = shutil.which("git")
    shim = tmp_path / "bin" / "git"
    shim.parent.mkdir()
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = cat-file ]; then echo "fatal: https://user:s3cr3t@host/repo" >&2; exit 3; fi\n'
        "done\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ['PATH']}")

    # when
    with pytest.raises(GitError) as raised:
        transport.generations_in_reachable_objects((commit,))

    # then
    assert "`git cat-file` failed (exit 3)" in str(raised.value)
    assert "s3cr3t" not in str(raised.value)
