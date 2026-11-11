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


def test_commit_message_sends_diff_and_cleans_reply(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    msgs = fake_client(monkeypatch, ai.CommitMessage(message='"Change a.txt to two"\nextra line'))
    msg, tel = ai.suggest_commit_message(git.state(repo))
    assert msg == "Change a.txt to two"
    kw = msgs.calls[0]
    assert kw["output_format"] is ai.CommitMessage and kw["system"] == ai.COMMIT_SYSTEM
    assert "+two" in kw["messages"][0]["content"]
    assert tel["input_tokens"] == 10


def test_commit_message_empty_reply(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    fake_client(monkeypatch, ai.CommitMessage(message="  "))
    with pytest.raises(ai.AIUnavailable, match="Type your own"):
        ai.suggest_commit_message(git.state(repo))


def test_summary_respects_opt_out(repo, monkeypatch):
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setenv("GITLY_AI_SEND_CODE", "0")
    msgs = fake_client(monkeypatch, ai.ChangeSummary(
        overview="You edited a.txt.", points=["Changed the text", " ", *[f"p{i}" for i in range(9)]]))
    out, _ = ai.summarize_changes(git.state(repo))
    content = msgs.calls[0]["messages"][0]["content"]
    assert "+two" not in content and "not shared" in content
    assert "a.txt" in content  # file names still come through repo_state
    assert out.points[0] == "Changed the text" and len(out.points) == 6


def test_new_schemas_are_accepted_by_sdk():
    from anthropic.lib._parse._transform import transform_schema
    for model in (ai.CommitMessage, ai.ChangeSummary):
        assert transform_schema(model.model_json_schema())["additionalProperties"] is False


def test_branch_name_sends_typed_text_and_context(repo, monkeypatch):
    (repo / "footer.css").write_text("footer { display: none }\n")
    msgs = fake_client(monkeypatch, ai.BranchName(name=' "fix-mobile-footer" ', reason="made it more specific"))
    out, _ = ai.suggest_branch_name("footer fix", git.state(repo), "where you are", ["main", "login"])
    assert out.name == "fix-mobile-footer"
    kw = msgs.calls[0]
    content = kw["messages"][0]["content"]
    assert kw["system"] == ai.BRANCH_SYSTEM and kw["output_format"] is ai.BranchName
    assert "<typed>\nfooter fix\n</typed>" in content
    assert "login" in content and "where you are" in content
    assert "display: none" in content  # unsaved changes give it context


def test_branch_name_skips_diff_when_clean(repo, monkeypatch):
    msgs = fake_client(monkeypatch, ai.BranchName(name="x", reason="kept"))
    ai.suggest_branch_name("x", git.state(repo), "where you are", [])
    assert "<changes>" not in msgs.calls[0]["messages"][0]["content"]
