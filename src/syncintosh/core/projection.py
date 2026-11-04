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
* :func:`restore_text` — the repo text with `live`'s local keys put back, rendered against the **repo**
  text so comments and layout that changed on the other machine come across, and verified by
  projecting the result back to exactly the repo text. That check is what keeps the apply
  ledger's digest from drifting on every cycle.

Put back means *where the live file had them*. Projecting the live file is a list of text edits, so
:func:`restore_text` first runs those edits backwards: each one is moved onto the repo text, as long as
the lines around it are the same there, and the live file's own text for the key — its position, its
trailing comment, its one-line layout — returns untouched. Only when a neighbouring line changed on the
other machine, or the result fails either check, does it fall back to :func:`graft` and a re-render, which
gets the values right but lays a re-attached key out as the renderer would.

A key the *user* chose to ignore is different, and is **carried** instead of stripped. Stripping it
would make the 3-way merge read it as deleted and erase the value from every other machine, so its
value in the sealed object comes from `Carry.source` — an object the repo already holds — and the
live value is put back on apply. The order is static strip → carry → static strip: the carried value
is a stored object, and may itself hold a key the preset excludes, which must not be sealed again.
With no source the carried key is left out of the sealed object (``docs/design/02-base-and-merge.md``).

Because rendering re-uses the format adapter's minimal-edit path, comments, key order, trailing
commas, and newline style survive on both halves (``AGENTS.md`` §2.5).

This module is pure: it takes text, a policy, and a `ContentAdapter` port, and returns text
(``AGENTS.md`` §2.1).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from syncintosh.core.errors import ContentParseError, ProjectionError
from syncintosh.core.ports import ContentAdapter, TextEdit, Verbatim
from syncintosh.core.presets import PresetFile
from syncintosh.core.tree import EmptyObject, KeyPath, Node, Tree


@dataclass(frozen=True)
class Carry:
    """The keys whose value is carried from an object already in the repo, and that object's text.

    The source is the **apply-ledger baseline object** — what this machine last accounted for — and
    not `HEAD`. Change detection compares the live projection with that baseline (``core/ledger.py``),
    so a carried key projects to the baseline's own value and a local change to it never registers;
    projecting with `HEAD` would make a key that only the *remote* moved look locally edited, and
    send an unchanged file through a stale-base merge on every cycle. `source` is `None` when no object is
    available, and the carried keys are then left out. When this machine has no ledger object, services pass the
    object at `HEAD` instead, so a file the repo already holds keeps its value (`services.sync._baseline_reader`).
    """

    paths: frozenset[KeyPath] = frozenset()
    source: str | None = field(default=None, repr=False)


NO_CARRY = Carry()


def _root_of(path: KeyPath, roots: Iterable[KeyPath]) -> KeyPath | None:
    """The first of `roots` that `path` is, or lies beneath."""
    return next((root for root in roots if path[: len(root)] == root), None)


def _under(path: KeyPath, roots: Iterable[KeyPath]) -> bool:
    """True when `path` is one of `roots` or lies strictly beneath it."""
    return _root_of(path, roots) is not None


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


def carry(tree: Tree, paths: frozenset[KeyPath], source: Tree | None) -> Tree:
    """`tree` with the entries at or under `paths` taken from `source` instead.

    Each carried key keeps the position of the one it replaces, so the renderer edits it in place;
    a key `tree` does not have is appended. A `source` that lacks the key removes it.
    """
    if not paths:
        return tree
    replacement: dict[KeyPath, dict[KeyPath, Node]] = {root: {} for root in paths}
    if source is not None:
        for path, node in source.entries.items():
            root = _root_of(path, paths)
            if root is not None:
                replacement[root][path] = node
    entries: dict[KeyPath, Node] = {}
    placed: set[KeyPath] = set()
    for path, node in tree.entries.items():
        root = _root_of(path, paths)
        if root is None:
            entries[path] = node
        elif root not in placed:
            placed.add(root)
            entries.update(replacement[root])
    for root, carried in replacement.items():
        if root not in placed:
            entries.update(carried)
    return _document(entries)


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


def project_text(adapter: ContentAdapter, text: str, policy: PresetFile, carried: Carry = NO_CARRY) -> str:
    """The text that is sealed for `policy`: `text` with noise and excluded keys removed.

    The rendered result is re-parsed and compared with the projected tree, because a renderer
    that half-applies an edit would otherwise seal a file that is neither the original nor the
    projection — and, for `excluded`, would seal a credential.

    `carried` swaps the value of each user-ignored key for the one in its source object. Strip runs
    again afterwards because that object may hold a key the preset excludes.
    """
    paths = policy.stripped
    if not paths and not carried.paths:
        return text
    tree, projected, verbatim = _projection(adapter, text, policy, carried)
    if projected == tree:
        return text
    rendered = _render(adapter, projected, text, policy, verbatim)
    if _parse(adapter, rendered, policy) != projected:
        raise ProjectionError(f"{policy.path}: stripping the preset's keys did not render cleanly")
    return rendered


def _projection(
    adapter: ContentAdapter, text: str, policy: PresetFile, carried: Carry
) -> tuple[Tree, Tree, Verbatim | None]:
    """`text`'s tree, the tree its projection has, and the values the renderer copies as written."""
    paths = policy.stripped
    tree = _parse(adapter, text, policy)
    source = None if carried.source is None else _parse(adapter, carried.source, policy)
    projected = strip(carry(strip(tree, paths), carried.paths, source), paths)
    # The carried values are copied as the source wrote them, not re-serialized: `1e3` must stay `1e3` for the
    # next cycle's projection of the same source to be this very text.
    verbatim = None if carried.source is None or not carried.paths else Verbatim(carried.source, carried.paths)
    return tree, projected, verbatim


def restore_text(
    adapter: ContentAdapter, repo_text: str, live_text: str, policy: PresetFile, carried: Carry = NO_CARRY
) -> str:
    """`repo_text` with `live_text`'s local values at `policy`'s paths put back.

    `live_text` is the file as the user has it; `repo_text` is the published object. The result
    is rendered against `repo_text`, then projected again and required to reproduce `repo_text`
    **byte for byte** — the apply ledger's digest is the projection's, so a restore that fails
    this check would make every later cycle read the file as changed.

    A carried key takes the live file's value too, absent or not: the key is the machine's own.
    The check projects with **`repo_text` as the carry source**, because it is the object being
    applied and so the next cycle's baseline; whatever `carried.source` was at collect is not.

    The live file's own text for those keys is kept when it can be (:func:`_rebased`). When it cannot, they
    are re-attached through the renderer, which places a key by its sibling, writes a value on one line as
    the format's default, and has no place for a trailing comment.
    """
    paths = policy.stripped | carried.paths
    if not paths:
        return repo_text
    live = _parse(adapter, live_text, policy)
    restored = graft(_parse(adapter, repo_text, policy), live, paths)
    rebased = _rebased(adapter, repo_text, live_text, restored, policy, carried)
    if rebased is not None:
        return rebased
    # A carried key comes back as this machine's file wrote it, for the same reason the carry above is copied.
    # Only those: a noise or excluded key is re-attached the ordinary way.
    verbatim = Verbatim(live_text, carried.paths) if carried.paths else None
    rendered = _render(adapter, restored, repo_text, policy, verbatim)
    if project_text(adapter, rendered, policy, Carry(carried.paths, repo_text)) != repo_text:
        raise ProjectionError(
            f"{policy.path}: re-attaching this machine's local keys did not round-trip; refusing to write"
        )
    return rendered


def _rebased(
    adapter: ContentAdapter, repo_text: str, live_text: str, restored: Tree, policy: PresetFile, carried: Carry
) -> str | None:
    """`repo_text` with the edits that projected `live_text` undone in place, or `None` when that is not safe.

    Projecting the live file is a list of edits to its text. Undoing each one at the matching spot of the repo
    text puts the live file's own bytes back — a key where it stood, the comment on its line — and leaves every
    line the other machine changed as it came. A spot only counts when the line it sits on and the lines on each
    side are the same in both texts (`_place`); an edit beside a changed line is not moved, since where the text
    belongs relative to that change is not something to guess.

    The result is accepted only if it holds exactly the values :func:`graft` would give and projects back to
    `repo_text` byte for byte, so this can change a layout but never a value. Any failure is `None`.
    """
    source = Carry(carried.paths, repo_text)
    try:
        tree, projected, verbatim = _projection(adapter, live_text, policy, source)
        edits = () if projected == tree else adapter.render_edits(projected, live_text, policy.array_rules, verbatim)
        shown = _apply(live_text, edits)
        placed = _place(shown, repo_text, _undo(live_text, edits))
        if placed is None:
            return None
        result = _apply(repo_text, placed)
        if _parse(adapter, result, policy) != restored:
            return None
        return result if project_text(adapter, result, policy, source) == repo_text else None
    except (ProjectionError, ContentParseError):
        return None


def _apply(text: str, edits: Sequence[TextEdit]) -> str:
    """`text` with `edits` applied; they are sorted and do not overlap."""
    pieces: list[str] = []
    cursor = 0
    for edit in edits:
        pieces.append(text[cursor : edit.start])
        pieces.append(edit.text)
        cursor = edit.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _undo(original: str, edits: Sequence[TextEdit]) -> list[TextEdit]:
    """The edits that turn the text `edits` produce back into `original`, in the produced text's offsets."""
    undo: list[TextEdit] = []
    shift = 0
    for edit in edits:
        start = edit.start + shift
        undo.append(TextEdit(start, start + len(edit.text), original[edit.start : edit.end]))
        shift += len(edit.text) - (edit.end - edit.start)
    return undo


def _place(shown: str, repo_text: str, edits: Sequence[TextEdit]) -> list[TextEdit] | None:
    """`edits` (offsets in `shown`) moved to the same spot in `repo_text`, or `None` when one has no safe spot.

    The two texts are matched line by line. Both get a marker line at each end, so the first and last lines of a
    file have a neighbour too, and a file the other machine extended at the end does not match.
    """
    if not edits:
        return []
    shown_lines = shown.splitlines(keepends=True)
    repo_lines = repo_text.splitlines(keepends=True)
    matcher = SequenceMatcher(None, [None, *shown_lines, None], [None, *repo_lines, None], autojunk=False)
    equal = [(i1, i2, j1) for tag, i1, i2, j1, _ in matcher.get_opcodes() if tag == "equal"]
    shown_starts = _line_starts(shown_lines)
    repo_starts = _line_starts(repo_lines)

    def locate(offset: int, first: int, last: int) -> int | None:
        """`offset` (in `shown`) as an offset in `repo_text`, for an edit that spans lines `first`..`last`."""
        # The end marker has no line after it; every other line needs both its neighbours.
        after = min(last + 1, len(shown_starts) + 1)
        block = next(((i1, j1) for i1, i2, j1 in equal if i1 <= first - 1 and after < i2), None)
        if block is None:
            return None
        line = _line_at(shown_starts, len(shown), offset)
        mapped = block[1] + (line - block[0])
        within = offset - (shown_starts[line - 1] if line <= len(shown_starts) else len(shown))
        return (repo_starts[mapped - 1] if mapped <= len(repo_starts) else len(repo_text)) + within

    placed: list[TextEdit] = []
    cursor = 0
    for edit in edits:
        first = _line_at(shown_starts, len(shown), edit.start)
        last = first if edit.end == edit.start else _line_at(shown_starts, len(shown), edit.end - 1)
        start = locate(edit.start, first, last)
        end = start if edit.end == edit.start or start is None else locate(edit.end - 1, first, last)
        if start is None or end is None or start < cursor:
            return None
        if edit.end != edit.start:
            end += 1
        placed.append(TextEdit(start, end, edit.text))
        cursor = end
    return placed


def _line_starts(lines: Sequence[str]) -> list[int]:
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    return starts


def _line_at(starts: Sequence[int], length: int, offset: int) -> int:
    """The index of the line holding `offset`, counting the marker line before the first as 0.

    The end of the text belongs to the marker line after the last one.
    """
    return len(starts) + 1 if offset >= length else bisect_right(starts, offset)


def _parse(adapter: ContentAdapter, text: str, policy: PresetFile) -> Tree:
    try:
        return adapter.parse(text)
    except ContentParseError as exc:
        raise ProjectionError(f"{policy.path}: not valid {policy.format} ({exc})") from exc


def _render(
    adapter: ContentAdapter, tree: Tree, template: str, policy: PresetFile, verbatim: Verbatim | None = None
) -> str:
    try:
        return adapter.render(tree, template, policy.array_rules, verbatim)
    except ContentParseError as exc:
        raise ProjectionError(f"{policy.path}: cannot rewrite the file safely ({exc})") from exc


__all__ = ["NO_CARRY", "Carry", "carry", "graft", "project_text", "restore_text", "strip"]
