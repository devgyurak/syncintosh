# SPDX-License-Identifier: AGPL-3.0-or-later
"""Use-case: decide how the conflicts a stopped sync left pending are settled.

`resolve` runs the sync cycle again (``services/sync.py``, `Resolution`), so the conflicts it settles
are the ones that exist now, not the ones remembered from the stop. A decision is made against a
conflict's fingerprint and is never stored: it lives for this run only (``docs/design/01-sync-model.md``).

Every way of deciding goes through one `decision`. A choice source (`take_chooser`, `scoped_chooser`)
only says what one conflict's choice is, or that it has none.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from syncintosh.core.advice import Advice, MaskedText, new_host_tokens, rationale_for_display
from syncintosh.core.errors import ConflictsNotCovered, ContentParseError, SyntaxInvalid
from syncintosh.core.keyed_array import accepts_group
from syncintosh.core.manifest import FileFormat
from syncintosh.core.paths import collision_key
from syncintosh.core.ports import ConflictAdvisor, ContentAdapter, Masker, PresetCatalog, SyntaxValidator
from syncintosh.core.presets import ProposedUrl, ValidatorName, proposed_urls
from syncintosh.core.resolution import Choice, ConflictView, Decide, NamedChoice, Picks, Proposal, Side
from syncintosh.core.tree import element_label, path_label

# Asked once the decisions are made, before anything is published: the conflicts and every pick the
# run would publish. Returning `False` stops the run with nothing published.
Approve = Callable[[tuple[ConflictView, ...], Picks], bool]

# Settles one conflict, or returns `None` when it has no choice for that conflict.
Chooser = Callable[[ConflictView], Choice | None]


def decision(choose: Chooser, *, approve: Approve | None = None) -> Decide:
    """The decision every resolve run uses.

    A conflict answered earlier in the run is not asked again. A conflict `choose` has no choice for
    stops the run before anything is published, and the error names what is left, so nothing is
    published on a partial decision. `approve` sees the conflicts and every pick the run would publish.
    """

    def decide(views: tuple[ConflictView, ...], answered: Picks) -> Picks | None:
        fresh: dict[str, Choice] = {}
        uncovered: list[ConflictView] = []
        for view in views:
            if view.fingerprint in answered:
                continue
            choice = choose(view)
            if choice is None:
                uncovered.append(view)
            else:
                fresh[view.fingerprint] = choice
        if uncovered:
            raise ConflictsNotCovered(_describe(uncovered))
        if not fresh:
            return None
        if approve is not None and not approve(views, {**answered, **fresh}):
            return None
        return fresh

    return decide


def take_chooser(side: Side) -> Chooser:
    """Every conflict goes to `side`."""

    def choose(view: ConflictView) -> Choice | None:
        return _take(view, side)

    return choose


def scoped_chooser(local: tuple[str, ...], remote: tuple[str, ...], *, home: str) -> Chooser:
    """Each conflict named by a path goes to that path's side; a conflict no path names has no choice."""
    sides: dict[str, Side] = {}
    for side, paths in (("local", local), ("remote", remote)):
        for raw in paths:
            key = collision_key(normalize_path(raw, home))
            if sides.get(key, side) != side:
                raise ValueError(f"{raw} is named for both sides")
            sides[key] = side

    def choose(view: ConflictView) -> Choice | None:
        return sides.get(collision_key(view.path))

    return choose


def normalize_path(raw: str, home: str) -> str:
    """A path as a conflict records it: relative to home, with `~` and an absolute home prefix removed.

    A relative path is taken as already relative to home. A shell that expanded `~` gives the absolute
    form, which is brought back to the same relative path.
    """
    # `~` means the home this run is for, not whatever HOME the process happens to have.
    expanded = raw[2:] if raw.startswith("~/") else raw
    if not Path(expanded).is_absolute():
        return str(PurePosixPath(expanded))
    prefix = home.rstrip("/") + "/"
    return expanded[len(prefix) :] if expanded.startswith(prefix) else expanded


def options_for(view: ConflictView) -> tuple[NamedChoice, ...]:
    """The choices a conflict offers, in the order a chooser lists them.

    An add/add and a path collision can also drop both claimants; a delete/modify keeps the
    surviving record or deletes it. A missing object is not listed at all: it has no view.
    """
    match view.kind:
        case "delete/modify":
            return ("keep", "delete")
        case "add/add" | "collision":
            return ("local", "remote", "drop")
        case _:
            return ("local", "remote")


def _take(view: ConflictView, side: Side) -> NamedChoice:
    if view.kind == "delete/modify":
        present = view.local_present if side == "local" else view.remote_present
        return "keep" if present else "delete"
    return side


def _describe(views: list[ConflictView]) -> str:
    return ", ".join(f"{view.path} ({view.kind})" for view in views)


__all__ = [
    "Approve",
    "Chooser",
    "Presenter",
    "Suggestion",
    "advised_chooser",
    "auto_apply_for",
    "decision",
    "normalize_path",
    "options_for",
    "proposal_problem",
    "scoped_chooser",
    "shell_presets",
    "take_chooser",
]


# --------------------------------------------------------------------------- assisted choice

# Only a content conflict can be proposed for. A record-level one (add/add, delete/modify, collision)
# is always the person's choice, whatever a model says (``docs/design/06-llm-assist.md``).
_CONTENT_KINDS: frozenset[str] = frozenset({"key", "file"})


@dataclass(frozen=True)
class Suggestion:
    """One model answer for one conflict, restored and pre-checked, for the person to accept or decline.

    `choice` is the decision accepting it would make: a side, or a `Proposal` holding the restored
    text. `refused` says why the answer cannot be accepted; a refused suggestion is still shown, so
    the person sees why it was not offered. `urls` are the URLs a proposal introduces, and each is
    untrusted. `rationale` is plaintext (its tokens restored), so `repr` never shows it.
    """

    view: ConflictView
    choice: Choice | None
    rationale: str = field(repr=False)
    urls: tuple[ProposedUrl, ...] = ()
    refused: str | None = None
    # True when the proposal leaves out a token of the masked conflict. The person sees it even under auto-apply.
    dropped: bool = False


# Shows one suggestion and returns whether the person accepts it. A refused suggestion is shown and
# declined without asking.
Presenter = Callable[[Suggestion], bool]


def advised_chooser(
    *,
    advisor: ConflictAdvisor,
    masker: Masker,
    fallback: Chooser,
    present: Presenter,
    auto_apply: Callable[[ConflictView], bool],
    check: Callable[[ConflictView, str], str | None],
) -> Chooser:
    """A chooser that asks the model first, and falls back to `fallback` whenever it has no usable answer.

    Each content conflict is masked, sent, and read back. An answer is restored and pre-checked before
    the person sees it, so a bad proposal never reaches the merge. A conflict is applied without asking
    only when `auto_apply` allows it, the answer is a usable choice, and it adds no URL; every other
    answer is shown to the person, who accepts it or falls through to `fallback`.
    """
    numbers = itertools.count()

    def choose(view: ConflictView) -> Choice | None:
        if view.kind not in _CONTENT_KINDS or not view.readable:
            return fallback(view)
        # The key path goes with the texts: a model choosing among `14`, `16` and `13` with no name for the
        # setting would be guessing, and an auto-applied guess is the worst outcome.
        key = "" if view.key_path is None else path_label(view.key_path)
        if view.element is not None:
            # One binding group of an array: the answer is an array of that group's bindings, not one value. The
            # `when` is sent as its text, not a digest; the masker covers it like any other text, and a digest of it
            # would be a fingerprint, which a model must never be sent.
            key += f" {element_label(view.element, when_text=True)} (a JSON array of this group's bindings)"
        path, key_text, base, local, remote = masker.mask(
            view.path, key, view.base_text, view.local_text, view.remote_text
        )
        advice = advisor.advise(next(numbers), path=path, key=key_text, base=base, local=local, remote=remote)
        if advice is None:
            return fallback(view)
        suggestion = _suggest(view, advice, base, masker, check)
        usable = suggestion.refused is None and suggestion.choice is not None
        if usable and not suggestion.urls and not suggestion.dropped and auto_apply(view):
            return suggestion.choice
        accepted = present(suggestion)
        if accepted and usable:
            return suggestion.choice
        return fallback(view)

    return choose


def _suggest(
    view: ConflictView,
    advice: Advice,
    masked: MaskedText,
    masker: Masker,
    check: Callable[[ConflictView, str], str | None],
) -> Suggestion:
    """The answer as a suggestion: its tokens restored, and a proposal checked before it is offered."""
    rationale = rationale_for_display(advice.rationale, masked.values)
    if advice.choice != "proposal":
        return Suggestion(view=view, choice=advice.choice, rationale=rationale)
    text = masker.unmask(advice.value or "", masked)
    if text is None:
        return Suggestion(
            view=view,
            choice=None,
            rationale=rationale,
            refused="the proposed value does not name each placeholder exactly once",
        )
    problem = check(view, text)
    if problem is not None:
        return Suggestion(view=view, choice=None, rationale=rationale, refused=problem)
    sides = (view.base_text, view.local_text, view.remote_text)
    urls = proposed_urls(text, base=sides[0], local=sides[1], remote=sides[2])
    # A host the proposal names that no side did is a new endpoint, whatever its spelling: a remote, a
    # proxy, or a pipe to `sh`. It is shown and asked for, as a new URL is.
    known_domains = {item.domain for item in urls}
    hosts = tuple(
        ProposedUrl(url=host, domain=host) for host in new_host_tokens(text, *sides) if host not in known_domains
    )
    dropped = any(token not in advice.value for token, _ in masked.values if advice.value is not None)
    return Suggestion(view=view, choice=Proposal(text), rationale=rationale, urls=(*urls, *hosts), dropped=dropped)


def proposal_problem(
    view: ConflictView,
    text: str,
    *,
    catalog: PresetCatalog,
    adapters: Mapping[FileFormat, ContentAdapter],
    validators: Mapping[ValidatorName, SyntaxValidator],
) -> str | None:
    """Why a proposed text cannot be a result for this conflict, or `None` when it may be offered.

    A key value is parsed as a fragment of its format. A whole file is parsed and then given the
    preset's syntax check. The merge and the publish check the same things again, so this only keeps
    a bad proposal from being offered; it is not the gate.
    """
    policy = catalog.policy_for(view.path)
    if policy is None:
        return None
    adapter = adapters.get(policy.format)
    if adapter is not None:
        try:
            adapter.parse(text)
        except ContentParseError:
            return f"the proposed value does not parse as {policy.format}"
    if view.element is not None and view.key_path is not None and adapter is not None:
        # A binding group takes only bindings of that group; a proposal that names another group would move one.
        rule = policy.array_rules.get(view.key_path)
        try:
            tree = adapter.parse(text)
        except ContentParseError:
            tree = None
        if rule is None or tree is None or not accepts_group(tree.value(()), rule, view.element):
            return "the proposed value is not an array of this binding's group"
    validator = validators.get(policy.validator) if policy.validator is not None else None
    if view.kind == "file" and validator is not None:
        try:
            validator.check(text, subject=view.path)
        except SyntaxInvalid:
            return "the proposed file fails its syntax check"
    return None


def shell_presets(catalog: PresetCatalog, names: Iterable[str]) -> tuple[str, ...]:
    """The named presets whose files a shell reads. A proposal to change shell code is never applied unasked."""
    shell: list[str] = []
    for name in names:
        preset = catalog.get(name)
        if preset is not None and any(file.validator == "zsh" for file in preset.files):
            shell.append(name)
    return tuple(shell)


def auto_apply_for(catalog: PresetCatalog, names: frozenset[str]) -> Callable[[ConflictView], bool]:
    """Allow automatic application for a content conflict of one of `names`' presets, except a shell preset."""
    allowed_names = names - set(shell_presets(catalog, names))

    def allowed(view: ConflictView) -> bool:
        if view.kind not in _CONTENT_KINDS:
            return False
        preset = catalog.preset_for(view.path)
        return preset is not None and preset.name in allowed_names

    return allowed
