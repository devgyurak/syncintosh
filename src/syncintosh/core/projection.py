# SPDX-License-Identifier: AGPL-3.0-or-later
"""Projection: strip the keys a preset excludes before sealing, re-attach them on apply.

A preset's `noise` and `excluded` keys never travel. They are removed **before the object is
encrypted**, so:

* a change to a noise key changes no digest, and therefore produces neither a diff nor a
  conflict — the endless "window moved" prompt the noise list exists to prevent;
* a credential excluded in code cannot reach the repo even if the file itself is tracked,
  because the bytes that are sealed do not contain it (``AGENTS.md`` §3.4).

Doing this in the merge (`merge_tree(..., ignore=)`) is **not** enough: the ignored value is
still written to the object, so a Docker `auths` block would be committed. The cut has to be on
the collect side, which is why this module exists beside the merge engine rather than inside it.

The two halves are inverses:

* :func:`project_text` — `render(strip(parse(text)))`, verified by re-parsing the result. This
  is what is sealed, hashed, and compared.
* :func:`restore_text` — `render(graft(parse(repo), parse(live)))`, rendered against the **repo**
  text so comments and layout that changed on the other machine come across, and verified by
  projecting the result back to exactly the repo text. That check is what keeps the apply
  ledger's digest from drifting on every cycle.

Because rendering re-uses the format adapter's minimal-edit path, comments, key order, trailing
commas, and newline style survive on both halves (``AGENTS.md`` §2.5).

This module is pure: it takes text, a policy, and a `ContentAdapter` port, and returns text
(``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from collections.abc import Iterable

from syncintosh.core.errors import ContentParseError, ProjectionError
from syncintosh.core.ports import ContentAdapter
from syncintosh.core.presets import PresetFile
from syncintosh.core.tree import EmptyObject, KeyPath, Node, Tree


def _under(path: KeyPath, roots: Iterable[KeyPath]) -> bool:
    """True when `path` is one of `roots` or lies strictly beneath it."""
    return any(path[: len(root)] == root for root in roots)


def _consistent(entries: dict[KeyPath, Node]) -> dict[KeyPath, Node]:
    """Drop an `EmptyObject` that gained a child, so the tree is a well-formed document.

    Stripping a key from an object that was `{}` in one branch and populated in another can
    leave an explicit `EmptyObject` above a surviving path. `{}` and `{...}` cannot both be the
    value of one key; the populated shape wins, exactly as the merge engine decides.
    """
    descendant_roots = set(entries)
    return {
        path: node
        for path, node in entries.items()
        if not (isinstance(node, EmptyObject) and _has_descendant(path, descendant_roots))
    }


def _has_descendant(path: KeyPath, paths: Iterable[KeyPath]) -> bool:
    return any(len(candidate) > len(path) and candidate[: len(path)] == path for candidate in paths)


def _document(entries: dict[KeyPath, Node]) -> Tree:
    """Freeze a path map, keeping "nothing left" as the empty object rather than *no* paths.

    A parser represents a document with nothing in it as an `EmptyObject` at the root
    (:mod:`syncintosh.core.tree`), and the renderer re-parses to compare. A projection that
    stripped every key must therefore be that same shape — a tree with no entries at all would
    render back to *no* tree, and the round-trip check could never agree. This is the case of a
    settings file whose only contents were machine-local or excluded keys.
    """
    kept = _consistent(entries)
    if not kept:
        return Tree.create({(): EmptyObject()})
    return Tree.create(kept)


def strip(tree: Tree, paths: frozenset[KeyPath]) -> Tree:
    """Return `tree` without the entries at or under `paths`.

    A parent object that loses every child simply has no path left, so it disappears — and
    :func:`graft` re-creates it when the local values come back.
    """
    if not paths:
        return tree
    kept = {path: node for path, node in tree.entries.items() if not _under(path, paths)}
    return _document(kept)


def graft(repo: Tree, live: Tree, paths: frozenset[KeyPath]) -> Tree:
    """`repo` with `live`'s values at `paths` re-attached, ordered by `live`.

    The ordering matters. The repo text was rendered **without** the stripped keys, so the
    renderer re-inserts each one relative to the sibling it already knows; taking the order from
    `live` (which has every key) puts each back exactly where the live file had it, instead of
    appending them all at the end of the file.

    A key that `live` has and `repo` does not — outside `paths` — was deleted by the merge, so
    it is dropped; a key `repo` has and `live` does not is carried over.
    """
    if not paths:
        return repo
    entries: dict[KeyPath, Node] = {}
    repo_entries = repo.entries
    for path, node in live.entries.items():
        if _under(path, paths):
            entries[path] = node
        elif path in repo_entries:
            entries[path] = repo_entries[path]
    for path, node in repo_entries.items():
        if not _under(path, paths):
            entries.setdefault(path, node)
    return _document(entries)


def project_text(adapter: ContentAdapter, text: str, policy: PresetFile) -> str:
    """The text that is sealed for `policy`: `text` with noise and excluded keys removed.

    The rendered result is re-parsed and compared with the projected tree, because a renderer
    that half-applies an edit would otherwise seal a file that is neither the original nor the
    projection — and, for `excluded`, would seal a credential.
    """
    paths = policy.stripped
    if not paths:
        return text
    tree = _parse(adapter, text, policy)
    projected = strip(tree, paths)
    if projected == tree:
        return text
    rendered = _render(adapter, projected, text, policy)
    if _parse(adapter, rendered, policy) != projected:
        raise ProjectionError(f"{policy.path}: stripping the preset's keys did not render cleanly")
    return rendered


def restore_text(adapter: ContentAdapter, repo_text: str, live_text: str, policy: PresetFile) -> str:
    """`repo_text` with `live_text`'s local values at `policy`'s paths put back.

    `live_text` is the file as the user has it; `repo_text` is the published object. The result
    is rendered against `repo_text`, then projected again and required to reproduce `repo_text`
    **byte for byte** — the apply ledger's digest is the projection's, so a restore that fails
    this check would make every later cycle read the file as changed.
    """
    paths = policy.stripped
    if not paths:
        return repo_text
    restored = graft(_parse(adapter, repo_text, policy), _parse(adapter, live_text, policy), paths)
    rendered = _render(adapter, restored, repo_text, policy)
    if project_text(adapter, rendered, policy) != repo_text:
        raise ProjectionError(
            f"{policy.path}: re-attaching this machine's local keys did not round-trip; refusing to write"
        )
    return rendered


def _parse(adapter: ContentAdapter, text: str, policy: PresetFile) -> Tree:
    try:
        return adapter.parse(text)
    except ContentParseError as exc:
        raise ProjectionError(f"{policy.path}: not valid {policy.format} ({exc})") from exc


def _render(adapter: ContentAdapter, tree: Tree, template: str, policy: PresetFile) -> str:
    try:
        return adapter.render(tree, template)
    except ContentParseError as exc:
        raise ProjectionError(f"{policy.path}: cannot rewrite the file safely ({exc})") from exc


__all__ = ["graft", "project_text", "restore_text", "strip"]
