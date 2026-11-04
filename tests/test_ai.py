"""AI layer tests with a stubbed client (no network, no API key needed)."""

from types import SimpleNamespace

import anthropic
import pytest

from gitly import ai, git


class FakeMessages:
    def __init__(self, parsed, stop="end_turn"):
        self.parsed, self.stop, self.calls = parsed, stop, []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=self.parsed, stop_reason=self.stop,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def fake_client(monkeypatch, parsed, stop="end_turn"):
    msgs = FakeMessages(parsed, stop)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=msgs))
    return msgs


def test_plan_conversion(repo, monkeypatch):
    out = ai.AIPlan(answer="Moving it.", title="Move commit", steps=[
        ai.AIStep(args=["git", "switch", "--create", "fix"], explanation="new branch"),
        ai.AIStep(args=["reset", "--hard", "HEAD~1"], explanation="drop from main"),
    ])
    msgs = fake_client(monkeypatch, out)
    plan, tel = ai.ask("I committed to the wrong branch", git.state(repo))
    assert plan.source == "ai" and plan.note == "Moving it."
    assert plan.steps[0].args == ["switch", "--create", "fix"]  # leading "git" stripped
    assert plan.risk == "danger"
    kw = msgs.calls[0]
    assert kw["model"] == ai.MODEL and kw["output_format"] is ai.AIPlan
    assert "branch: main" in kw["messages"][0]["content"]
    assert "first" in kw["messages"][0]["content"]  # recent commits included


def test_refusal(repo, monkeypatch):
    fake_client(monkeypatch, None, stop="refusal")
    with pytest.raises(ai.AIUnavailable, match="declined"):
        ai.ask("x", git.state(repo))


def test_truncated(repo, monkeypatch):
    fake_client(monkeypatch, None, stop="max_tokens")
    with pytest.raises(ai.AIUnavailable, match="cut off"):
        ai.ask("x", git.state(repo))


def test_schema_is_accepted_by_sdk():
    from anthropic.lib._parse._transform import transform_schema
    schema = transform_schema(ai.AIPlan.model_json_schema())
    assert schema["additionalProperties"] is False
