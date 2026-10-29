# SPDX-License-Identifier: AGPL-3.0-or-later
"""A content conflict settles across rounds: a pick can expose a new conflict, and that one takes its own pick too."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, cast

from syncintosh.core.content_merge import ContentMerge
from syncintosh.services.sync import _MAX_SETTLE_ROUNDS, _settle_content, _Settled

# `syncintosh.services.sync` is also a function name in the package, so the module is taken from `sys.modules`.
_SYNC = sys.modules["syncintosh.services.sync"]


def _chain_merge(chain: list[str]) -> Any:
    """A merge fake that conflicts on the first key of `chain` not yet chosen, and is clean once all are chosen.

    It stands in for a merge whose conflicts only appear after the earlier picks are applied, which is what the rounds
    exist for.
    """

    def merge(base, local, remote, driver, adapter, *, choice=None, key_choices=None, arrays=None, element_picks=None):
        chosen = key_choices or {}
        remaining = [key for key in chain if (key,) not in chosen]
        if not remaining:
            return ContentMerge(data=b"merged", conflicted=False)
        return ContentMerge(data=local, conflicted=True, conflict_addresses=(((remaining[0],), None),))

    return merge


def _views(addresses: Any) -> list[Any]:
    """One view per conflict address, fingerprinted by its key so a pick can name it."""
    return [SimpleNamespace(fingerprint=f"f{path[0]}", key_path=path, element=element) for path, element in addresses]


def _presets() -> Any:
    return SimpleNamespace(adapter_for=lambda fmt: None, policy_for=lambda path: None, when_tag=None)


def _settle(monkeypatch: Any, merge: Any, picks: Any) -> Any:
    monkeypatch.setattr(_SYNC, "merge_content", merge)
    monkeypatch.setattr(_SYNC, "_content_views", lambda *args: _views(args[-1]))
    monkeypatch.setattr(_SYNC, "_checked", lambda presets, data, *rest: _Settled(data=data))
    return _settle_content(
        presets=_presets(),
        driver=cast(Any, None),
        file_id="file",
        path="settings.json",
        fmt="jsonc",
        base=b"base",
        local=b"local",
        remote=b"remote",
        picks=picks,
        reason="the same region changed on both sides",
    )


def test_a_pick_that_exposes_a_new_conflict_is_settled_in_the_same_run(monkeypatch: Any) -> None:
    # given — settling `a` exposes `b`; the person has picked a side for both
    merge = _chain_merge(["a", "b"])

    # when
    settled = _settle(monkeypatch, merge, {"fa": "local", "fb": "remote"})

    # then — one run settles the file; nothing is left pending
    assert settled.data == b"merged"
    assert settled.detail == ""


def test_a_pick_that_never_stops_exposing_conflicts_stops_at_the_round_cap(monkeypatch: Any) -> None:
    # given — every conflict is new after the last pick, and the person has a pick for each of a hundred of them
    chain = [f"k{index}" for index in range(100)]
    picks = {f"f{key}": "local" for key in chain}
    calls: list[int] = []
    inner = _chain_merge(chain)

    def counting(*args: Any, **kwargs: Any) -> ContentMerge:
        calls.append(1)
        return inner(*args, **kwargs)

    # when
    settled = _settle(monkeypatch, counting, picks)

    # then — the first merge plus one merge per round, and no more: the loop stops at its cap; the file stays pending,
    # and the detail names the conflict the last round reached, which is the first one no pick settled
    assert len(calls) == 1 + _MAX_SETTLE_ROUNDS
    assert settled.data is None
    assert settled.detail.endswith(f": k{_MAX_SETTLE_ROUNDS}")
