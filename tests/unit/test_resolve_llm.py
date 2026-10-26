# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the assisted chooser: what is asked, what is offered, and what falls back to the person."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from syncintosh.adapters.jsonc import JsoncAdapter
from syncintosh.cli.main import app
from syncintosh.core.advice import Advice
from syncintosh.core.errors import SyntaxInvalid
from syncintosh.core.resolution import ConflictKind, ConflictView, Proposal
from syncintosh.presets import BuiltinCatalog
from syncintosh.services.resolve import advised_chooser, auto_apply_for, proposal_problem
from syncintosh.system.masking import TextMasker

VSCODE = "Library/Application Support/Code/User/settings.json"


class ReplyAdvisor:
    """Records each conflict it is asked about and returns one canned answer."""

    def __init__(self, advice: Advice | None) -> None:
        self.advice = advice
        self.asked: list[str] = []
        self.keys: list[str] = []

    def advise(self, index, *, path, key, base, local, remote):
        self.asked.append(local.text)
        self.keys.append(key.text)
        return self.advice


class Recorder:
    """A presenter that accepts or declines, and records what it was shown."""

    def __init__(self, accept: bool) -> None:
        self.accept = accept
        self.shown: list = []

    def __call__(self, suggestion) -> bool:
        self.shown.append(suggestion)
        return self.accept


def _view(kind: ConflictKind = "key", path: str = VSCODE, local: str = '{"a": 1}\n') -> ConflictView:
    return ConflictView(
        kind=kind,
        file_id="f" * 32,
        path=path,
        key_path=("a",) if kind == "key" else None,
        fingerprint="x" * 64,
        base_text='{"a": 0}\n',
        local_text=local,
        remote_text='{"a": 2}\n',
    )


def _fallback(view: ConflictView):
    return "remote"


def _chooser(advisor, present, *, auto=False, check=lambda view, text: None):
    return advised_chooser(
        advisor=advisor,
        masker=TextMasker(home="/Users/alice", username="alice", internal_domains=()),
        fallback=_fallback,
        present=present,
        auto_apply=lambda view: auto,
        check=check,
    )


def test_a_record_level_conflict_is_never_sent_to_the_model() -> None:
    # given
    advisor = ReplyAdvisor(Advice(choice="local"))

    # when
    choice = _chooser(advisor, Recorder(accept=True))(_view(kind="add/add"))

    # then — the person chooses, and the model is not asked
    assert advisor.asked == []
    assert choice == "remote"


def test_no_usable_answer_falls_back_to_the_person() -> None:
    # when
    choice = _chooser(ReplyAdvisor(None), Recorder(accept=True))(_view())

    # then
    assert choice == "remote"


def test_an_accepted_proposal_is_restored_and_returned() -> None:
    # given
    advisor = ReplyAdvisor(Advice(choice="proposal", value='{"a": 3}', rationale="merge"))
    presented = Recorder(accept=True)

    # when
    choice = _chooser(advisor, presented)(_view())

    # then
    assert choice == Proposal('{"a": 3}')
    assert presented.shown[0].rationale == "merge"


def test_a_declined_suggestion_falls_back_to_the_person() -> None:
    # given
    advisor = ReplyAdvisor(Advice(choice="local"))

    # when
    choice = _chooser(advisor, Recorder(accept=False))(_view())

    # then
    assert choice == "remote"


def test_a_side_suggestion_is_offered_as_that_side() -> None:
    # when
    choice = _chooser(ReplyAdvisor(Advice(choice="local")), Recorder(accept=True))(_view())

    # then
    assert choice == "local"


def test_a_proposal_that_fails_the_check_is_shown_refused_and_never_returned() -> None:
    # given
    advisor = ReplyAdvisor(Advice(choice="proposal", value='{"a": ', rationale="oops"))
    presented = Recorder(accept=True)

    # when
    choice = _chooser(advisor, presented, check=lambda view, text: "does not parse")(_view())

    # then — shown with its reason, never offered, and the person decides by hand
    assert choice == "remote"
    assert presented.shown[0].refused == "does not parse"


def test_a_proposal_with_a_token_the_masked_text_lacked_is_refused() -> None:
    # given — the placeholder names an index that the masked conflict never had
    advisor = ReplyAdvisor(Advice(choice="proposal", value="<<snt:00000000:9>>", rationale="x"))
    presented = Recorder(accept=True)

    # when
    choice = _chooser(advisor, presented)(_view())

    # then
    assert choice == "remote"
    assert "exactly once" in presented.shown[0].refused


def test_an_allowed_proposal_with_no_new_url_is_applied_without_asking() -> None:
    # given
    presented = Recorder(accept=False)
    advisor = ReplyAdvisor(Advice(choice="proposal", value='{"a": 3}', rationale="merge"))

    # when
    choice = _chooser(advisor, presented, auto=True)(_view())

    # then — allowed by the preset list and no URL, so no question is asked
    assert choice == Proposal('{"a": 3}')
    assert presented.shown == []


def test_a_proposal_that_adds_a_url_is_always_asked_even_when_auto_is_allowed() -> None:
    # given
    advisor = ReplyAdvisor(Advice(choice="proposal", value='{"a": "https://evil.example/x"}', rationale="link"))
    presented = Recorder(accept=True)

    # when
    choice = _chooser(advisor, presented, auto=True)(_view())

    # then
    assert presented.shown[0].urls[0].domain == "evil.example"
    assert presented.shown[0].urls[0].trusted is False
    assert choice == Proposal('{"a": "https://evil.example/x"}')


def test_a_url_from_the_model_is_untrusted_even_when_it_matches_a_shipped_one() -> None:
    # given — the same string a preset ships, arriving from the model
    advisor = ReplyAdvisor(Advice(choice="proposal", value="https://code.visualstudio.com/", rationale="docs"))
    presented = Recorder(accept=True)

    # when
    _chooser(advisor, presented)(_view())

    # then — flagged as new and untrusted, so the person is shown the domain
    assert [item.url for item in presented.shown[0].urls] == ["https://code.visualstudio.com/"]
    assert presented.shown[0].urls[0].trusted is False


@pytest.mark.parametrize(
    ("kind", "path", "names", "allowed"),
    [
        ("key", VSCODE, frozenset({"vscode"}), True),
        ("file", VSCODE, frozenset({"vscode"}), True),
        ("add/add", VSCODE, frozenset({"vscode"}), False),
        ("key", VSCODE, frozenset({"zsh"}), False),
        ("key", "not/a/tracked/path", frozenset({"vscode"}), False),
    ],
)
def test_auto_apply_is_limited_to_content_conflicts_of_listed_presets(kind, path, names, allowed) -> None:
    # when & then
    assert auto_apply_for(BuiltinCatalog.default(), names)(_view(kind=kind, path=path)) is allowed


def test_a_proposal_that_does_not_parse_as_its_format_is_refused() -> None:
    # when
    problem = proposal_problem(
        _view(),
        '{"a": ',
        catalog=BuiltinCatalog.default(),
        adapters={"jsonc": JsoncAdapter()},
        validators={},
    )

    # then
    assert problem == "the proposed value does not parse as jsonc"


def test_a_whole_file_that_fails_its_syntax_check_is_refused() -> None:
    # given
    class Refuses:
        def check(self, text: str, *, subject: str) -> None:
            raise SyntaxInvalid("no")

    # when
    problem = proposal_problem(
        _view(kind="file", path=".zshrc"),
        "if",
        catalog=BuiltinCatalog.default(),
        adapters={},
        validators={"zsh": Refuses()},
    )

    # then
    assert problem == "the proposed file fails its syntax check"


def test_resolve_llm_cannot_be_combined_with_a_side_choice() -> None:
    # when
    result = CliRunner().invoke(app, ["resolve", "--llm", "--take", "local"])

    # then
    assert result.exit_code == 2
    assert "--llm chooses for you" in result.output


def test_a_refused_suggestion_is_shown_and_never_prompted(monkeypatch, capsys) -> None:
    # given — a refused suggestion must not reach the accept prompt
    from syncintosh.cli.main import _present_suggestion
    from syncintosh.services.resolve import Suggestion

    prompted: list[str] = []
    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: prompted.append("asked") or True)
    suggestion = Suggestion(view=_view(), choice=None, rationale="it is a merge", refused="does not parse")

    # when
    accepted = _present_suggestion(suggestion)

    # then
    assert accepted is False
    assert prompted == []
    assert "does not parse" in capsys.readouterr().out


def test_a_suggestion_with_a_new_url_shows_it_as_untrusted_before_the_accept_prompt(monkeypatch, capsys) -> None:
    # given
    from syncintosh.cli.main import _present_suggestion
    from syncintosh.core.presets import proposed_urls
    from syncintosh.services.resolve import Suggestion

    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: False)
    text = '{"a": "https://evil.example/x"}\n'
    suggestion = Suggestion(
        view=_view(),
        choice=Proposal(text),
        rationale="link",
        urls=proposed_urls(text, base="", local="", remote=""),
    )

    # when
    accepted = _present_suggestion(suggestion)

    # then
    assert accepted is False
    assert "UNTRUSTED evil.example" in capsys.readouterr().out


def test_a_public_endpoint_is_confirmed_before_any_conflict_is_sent(monkeypatch) -> None:
    # given — the person declines the confirmation for a public host
    from syncintosh.cli.main import _assisted_choice
    from syncintosh.core.config import LlmConfig, SyncConfig

    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: False)
    config = SyncConfig.create(
        remote="r", machine_id="m", items=(), llm=LlmConfig.create(base_url="https://api.example.com/v1", model="m")
    )

    # when & then — exits before the ports or the model are touched
    with pytest.raises(typer.Exit):
        _assisted_choice(config, None, {})


def test_resolve_llm_needs_a_configured_endpoint() -> None:
    # given — no base_url or model is set
    from syncintosh.cli.main import _assisted_choice
    from syncintosh.core.config import SyncConfig

    config = SyncConfig.create(remote="r", machine_id="m", items=())

    # when & then
    with pytest.raises(typer.BadParameter, match=r"llm\.base_url"):
        _assisted_choice(config, None, {})


def test_a_proposal_for_an_untracked_path_has_no_pre_check() -> None:
    # when & then — no preset owns the path, so there is nothing to check here; the merge still checks
    assert (
        proposal_problem(
            _view(path="not/tracked.json"),
            "anything",
            catalog=BuiltinCatalog.default(),
            adapters={},
            validators={},
        )
        is None
    )


def test_a_binary_conflict_is_never_sent_and_never_proposed_for() -> None:
    # given — a side that is not text: its "(binary)" marker is not something a model should settle
    advisor = ReplyAdvisor(Advice(choice="proposal", value="text", rationale="x"))
    binary = ConflictView(
        kind="file",
        file_id="f" * 32,
        path="Library/Fonts/x.ttf",
        key_path=None,
        fingerprint="x" * 64,
        readable=False,
        local_text="(binary)",
        remote_text="(binary)",
    )

    # when
    choice = _chooser(advisor, Recorder(accept=True))(binary)

    # then
    assert advisor.asked == []
    assert choice == "remote"


def test_the_key_path_is_sent_with_the_texts() -> None:
    # given — the view is a key conflict at key path ("a",)
    advisor = ReplyAdvisor(Advice(choice="local"))

    # when
    _chooser(advisor, Recorder(accept=True))(_view())

    # then
    assert advisor.keys == ["a"]


def test_the_approval_screen_shows_a_proposed_value_in_full() -> None:
    # given — a proposal with a carriage return and a bidi override that must not hide anything
    from syncintosh.cli.main import _pick_lines

    # when
    lines = _pick_lines(_view(), Proposal('{"a": "x\u202ey"}'))

    # then — shown as its text, marked as the model's, with the override escaped
    assert "proposed by the model" in lines[0]
    assert "\\u202e" in lines[1]
    assert "Proposal" not in "".join(lines)


def test_a_file_proposal_is_shown_as_a_diff_on_the_approval_screen() -> None:
    # given
    from syncintosh.cli.main import _pick_lines

    view = _view(kind="file")

    # when
    lines = _pick_lines(view, Proposal('{"a": 3}\n'))

    # then
    assert any(line.strip().startswith("-") or line.strip().startswith("+") for line in lines[1:])


def test_a_plain_http_endpoint_to_a_public_host_is_refused_at_resolve_time() -> None:
    # given
    from syncintosh.cli.main import _assisted_choice
    from syncintosh.core.config import LlmConfig, SyncConfig

    config = SyncConfig.create(
        remote="r", machine_id="m", items=(), llm=LlmConfig.create(base_url="http://api.example.com/v1", model="m")
    )

    # when & then — refused before any call, and named as the reason
    with pytest.raises(typer.BadParameter, match="plain http"):
        _assisted_choice(config, None, {})
