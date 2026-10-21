# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for collect-side projection: strip → seal, restore → apply.

The keyed half runs against the JSONC adapter — the format with both key paths and comments — a
synthetic policy, and hypothesis over generated documents (``AGENTS.md`` §5.3).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.core.errors import ProjectionError
from syncintosh.core.presets import PresetFile
from syncintosh.core.projection import graft, project_text, restore_text, strip
from syncintosh.core.tree import Tree, tree_from_value

ADAPTER = JsoncAdapter()

NOISE = ("window", "zoomLevel")
EXCLUDED = ("auth",)

SETTINGS = (
    "{\n"
    "  // keep me\n"
    '  "editor": {\n'
    '    "fontSize": 13,\n'
    '    "wordWrap": "on"\n'
    "  },\n"
    '  "auth": {\n'
    '    "token": "SECRET"\n'
    "  },\n"
    '  "window": {\n'
    '    "zoomLevel": 1\n'
    "  }\n"
    "}\n"
)


def _policy(path: str = "settings.json", *, noise=(), excluded=()) -> PresetFile:
    return PresetFile.create(path=path, format="jsonc", noise=noise, excluded=excluded)


def _project(text: str, policy: PresetFile) -> str:
    return project_text(ADAPTER, text, policy)


# --------------------------------------------------------------------------- strip / graft


def test_strip_removes_a_path_and_everything_under_it() -> None:
    # given
    tree = tree_from_value({"auth": {"token": "x"}, "editor": {"fontSize": 13}})

    # when
    result = strip(tree, frozenset({("auth",)}))

    # then
    assert list(result) == [("editor", "fontSize")]


def test_strip_keeps_an_empty_object_when_every_key_is_removed() -> None:
    # given — a settings file whose only contents are stripped, as a Docker config.json can be
    tree = tree_from_value({"auth": {"token": "x"}})

    # when
    result = strip(tree, frozenset({("auth",)}))

    # then — the empty *document*, not a tree with no entries at all
    assert result.to_value() == {}


def test_strip_is_a_no_op_for_an_absent_path() -> None:
    # given
    tree = tree_from_value({"a": 1})

    # when
    result = strip(tree, frozenset({("nowhere",)}))

    # then
    assert result == tree


def test_strip_with_no_paths_returns_the_same_tree() -> None:
    # given
    tree = tree_from_value({"a": 1})

    # when & then
    assert strip(tree, frozenset()) is tree


def test_graft_keeps_the_live_order_and_drops_a_key_the_repo_deleted() -> None:
    # given — the live file has its own order, and a key the repo no longer carries
    repo = tree_from_value({"a": 1, "b": 2})
    live = tree_from_value({"b": 2, "a": 1, "noise": 9})

    # when
    result = graft(repo, live, frozenset({("noise",)}))

    # then
    assert list(result) == [("b",), ("a",), ("noise",)]
    assert result.value(("noise",)) == live.value(("noise",))


def test_graft_carries_a_repo_only_key_along_and_drops_a_live_only_one() -> None:
    # given — `added` is a key the other machine added; `local` is one the merge deleted
    repo = tree_from_value({"added": True})
    live = tree_from_value({"local": 1})

    # when
    result = graft(repo, live, frozenset({("noise",)}))

    # then — the repo is the authority outside the preset's paths
    assert result.to_value() == {"added": True}


def test_graft_drops_an_empty_object_once_a_child_returns() -> None:
    # given — the repo holds the parent as `{}`; the live file has a stripped key inside it
    repo = tree_from_value({"a": {}})
    live = tree_from_value({"a": {"noise": 1}})

    # when
    result = graft(repo, live, frozenset({("a", "noise")}))

    # then — `{}` and `{...}` cannot both be the value of one key
    assert list(result) == [("a", "noise")]


# --------------------------------------------------------------------------- project


def test_project_text_strips_noise_and_excluded_and_keeps_comments() -> None:
    # when
    result = _project(SETTINGS, _policy(noise={NOISE}, excluded={EXCLUDED}))

    # then
    assert "SECRET" not in result
    assert "zoomLevel" not in result
    assert "// keep me" in result
    assert ADAPTER.parse(result).to_value() == {"editor": {"fontSize": 13, "wordWrap": "on"}}


def test_project_text_returns_the_original_when_no_key_matches() -> None:
    # when
    result = _project(SETTINGS, _policy(noise={("nope",)}))

    # then — byte for byte, so an unrelated file is never rewritten
    assert result == SETTINGS


def test_project_text_is_the_identity_for_a_policy_with_no_paths() -> None:
    # given — the zsh policy: a validator, no keys to strip
    policy = PresetFile.create(path=".zshrc", format="text", validator="zsh")

    # when
    result = project_text(ADAPTER, "echo hi\n", policy)

    # then — no parse and no render, so no adapter is needed
    assert result == "echo hi\n"


def test_project_text_leaves_the_empty_object_when_every_key_is_stripped() -> None:
    # given
    text = '{\n  "auth": {\n    "token": "SECRET"\n  }\n}\n'

    # when
    result = _project(text, _policy(excluded={EXCLUDED}))

    # then — the file is sealed as an empty object, and the credential is gone
    assert "SECRET" not in result
    assert ADAPTER.parse(result).to_value() == {}


def test_project_text_refuses_a_file_it_cannot_parse() -> None:
    # when & then — fail-closed: the raw file is never sealed as a fallback
    with pytest.raises(ProjectionError):
        _project("{ not jsonc", _policy(noise={NOISE}))


def test_project_text_refuses_a_rewrite_that_would_drop_a_comment() -> None:
    # given — a one-line object, so removing a key means rewriting the whole value
    text = '{ /* keep */ "a": 1, "n": 2 }'

    # when & then
    with pytest.raises(ProjectionError):
        _project(text, _policy(noise={("n",)}))


# --------------------------------------------------------------------------- restore


def test_restore_text_re_attaches_the_local_values_and_reprojects_to_the_repo() -> None:
    # given — the repo object is the projection; the live file is the user's fuller settings
    policy = _policy(noise={NOISE}, excluded={EXCLUDED})
    repo_text = _project(SETTINGS, policy)
    live_text = SETTINGS.replace('"SECRET"', '"LOCAL-ONLY"').replace('"zoomLevel": 1', '"zoomLevel": 9')

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, policy)

    # then — the local values come back, the comment survives, and the result projects back to
    # the repo text byte for byte (the apply-ledger invariant)
    assert "LOCAL-ONLY" in restored
    assert '"zoomLevel": 9' in restored
    assert "// keep me" in restored
    assert _project(restored, policy) == repo_text


def test_restore_text_is_the_identity_when_a_policy_strips_nothing() -> None:
    # given
    policy = PresetFile.create(path=".zshrc", format="text", validator="zsh")

    # when & then
    assert restore_text(ADAPTER, "echo repo\n", "echo live\n", policy) == "echo repo\n"


# --------------------------------------------------------------------------- properties

_LEAVES = st.none() | st.booleans() | st.integers() | st.text(max_size=6)
_OBJECTS = st.dictionaries(st.text(min_size=1, max_size=6), _LEAVES, max_size=4)

_STRIPPED = _policy(noise={("noise",)}, excluded={("secret",)})


# A document on disk is laid out however its owner wrote it: member-per-line, or all on one line
# (an `echo '{...}' > file` edit). Both are ordinary, and only the second is where a naive render
# would re-flow the file and break the round-trip the apply ledger depends on.
_INDENT = st.sampled_from([None, 2])


def _document(value: dict, noise: object, secret: object, indent: int | None = None) -> str:
    return json.dumps({**value, "noise": noise, "secret": secret}, indent=indent, ensure_ascii=False)


@given(value=_OBJECTS, indent=_INDENT)
def test_a_projection_is_idempotent(value, indent) -> None:
    # when
    once = _project(json.dumps(value, indent=indent), _STRIPPED)
    twice = _project(once, _STRIPPED)

    # then
    assert twice == once


@given(value=_OBJECTS, before=st.integers(), after=st.integers(), indent=_INDENT)
def test_a_change_to_a_stripped_key_does_not_change_the_projection(value, before, after, indent) -> None:
    # when — the two documents differ only in the values of stripped keys
    first = _project(_document(value, before, "one", indent), _STRIPPED)
    second = _project(_document(value, after, "two", indent), _STRIPPED)

    # then — no digest changes, so no diff and no conflict
    assert first == second


@given(value=_OBJECTS, local_noise=st.integers(), indent=_INDENT)
def test_restore_always_projects_back_to_the_repo_text(value, local_noise, indent) -> None:
    # given — the repo holds the projection; this machine's file is the fuller document
    repo_text = _project(_document(value, 1, "SEALED", indent), _STRIPPED)
    live_text = _document(value, local_noise, "LOCAL", indent)

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, _STRIPPED)

    # then
    assert _project(restored, _STRIPPED) == repo_text
    assert '"LOCAL"' in restored


def test_a_one_line_repo_text_still_re_attaches_the_local_key() -> None:
    # given — the published object is one line, and this machine's file has a stripped key inline
    policy = _policy(noise={NOISE}, excluded={EXCLUDED})
    repo_text = '{"editor.fontSize": 13}\n'
    live_text = '{"editor.fontSize": 13, "window": {"zoomLevel": 9}}\n'

    # when
    restored = restore_text(ADAPTER, repo_text, live_text, policy)

    # then — the key is spliced in rather than re-flowing the object, so the reprojection is
    # byte-exact and the file is not reported unprojectable on every later cycle
    assert restored == '{"editor.fontSize": 13, "window": {"zoomLevel": 9}}\n'
    assert _project(restored, policy) == repo_text


def test_an_empty_one_line_repo_text_still_re_attaches_the_local_key() -> None:
    # given — `{}`, what an untouched `daemon.json` or a freshly created settings file is
    policy = _policy(excluded={EXCLUDED})
    live_text = '{"auth": {"token": "SECRET"}}\n'

    # when
    restored = restore_text(ADAPTER, "{}\n", live_text, policy)

    # then
    assert restored == '{"auth": {"token": "SECRET"}}\n'
    assert _project(restored, policy) == "{}\n"


def test_restore_text_refuses_an_object_that_is_not_a_projection() -> None:
    # given — the published object still holds the excluded key, which happens when the other
    # machine's build does not exclude it, and this machine's file holds its own value
    policy = _policy(excluded={EXCLUDED})
    repo_text = '{"auth": {"token": "FROM-AN-OLD-PUBLISH"}}'
    live_text = '{"auth": {"token": "LOCAL"}}'

    # when & then — refused, loudly (``unprojectable`` plus exit 1), rather than written: the two
    # builds disagree about what the file should hold, and applying would start a churn of commits
    # as each side re-published its own view. The user can act on a reported file; a silent
    # disagreement between builds is the failure mode this check exists to prevent.
    with pytest.raises(ProjectionError):
        restore_text(ADAPTER, repo_text, live_text, policy)


def test_restore_text_still_refuses_a_render_that_breaks_a_projection() -> None:
    # given — a renderer that silently loses a member, standing in for a renderer bug
    class Losing:
        def parse(self, text: str) -> Tree:
            return ADAPTER.parse(text)

        def render(self, tree: Tree, template: str) -> str:
            return "{}"

    policy = _policy(noise={NOISE})
    repo_text = '{"editor.fontSize": 13}'

    # when & then — the object *is* a projection, so a render that does not reproduce it is refused
    with pytest.raises(ProjectionError):
        restore_text(Losing(), repo_text, '{"editor.fontSize": 13, "window": {"zoomLevel": 9}}', policy)
