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


def test_saved_key_used_when_no_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not ai.available()
    path = ai.save_key("  sk-ant-test\n")
    assert path == tmp_path / "gitly" / "api_key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert ai.available() and ai.saved_key() == "sk-ant-test"

    seen = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: seen.update(kw) or SimpleNamespace(
        messages=FakeMessages(ai.CommitMessage(message="x"))))
    ai._call("sys", "hi", ai.CommitMessage, 100)
    assert seen["api_key"] == "sk-ant-test"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")  # the environment wins
    seen.clear()
    ai._call("sys", "hi", ai.CommitMessage, 100)
    assert "api_key" not in seen


def test_rejected_saved_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    ai.save_key("sk-ant-bad")

    def reject(**kw):
        raise anthropic.AuthenticationError("bad", response=SimpleNamespace(
            status_code=401, headers={}, request=None), body=None)

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(
        messages=SimpleNamespace(parse=reject)))
    with pytest.raises(ai.AIKeyRejected):
        ai._call("sys", "hi", ai.CommitMessage, 100)


def test_next_step_request(repo, monkeypatch):
    (repo / "a.txt").write_text("edited\n")
    out = ai.NextStep(action=" Save ", reason="One change.", message="Edit a", groups=[], branch="")
    msgs = fake_client(monkeypatch, out)
    step, tel = ai.suggest_next(git.state(repo), "<allowed_actions>\nsave\n</allowed_actions>", with_changes=True)
    kw = msgs.calls[0]
    assert step.action == "save" and kw["output_format"] is ai.NextStep and kw["system"] == ai.NEXT_SYSTEM
    content = kw["messages"][0]["content"]
    assert "+edited" in content and "<allowed_actions>" in content
    ai.suggest_next(git.state(repo), "facts", with_changes=False)
    assert "+edited" not in msgs.calls[1]["messages"][0]["content"]


def test_next_step_schema_is_accepted_by_sdk():
    from anthropic.lib._parse._transform import transform_schema
    assert transform_schema(ai.NextStep.model_json_schema())["additionalProperties"] is False


def test_default_model_is_sonnet():
    import os
    assert ai.MODEL == os.environ.get("GITLY_MODEL", "claude-sonnet-5-5")


def _conflict_file():
    from gitly import conflicts
    f = conflicts.ConflictFile("f.py", "UU", [
        conflicts.Spot("f.py", 1, "x = 3\n", "x = 2\n", line=4, before="def go():\n"),
        conflicts.Spot("f.py", 2, "a()\n", "b()\n", line=9),
        conflicts.Spot("f.py", 3, "big\n" * 2000, "small\n", line=20),  # too big to send
    ])
    sd = conflicts.Sides("main", "login", "merge")
    return f, sd


def test_conflict_advice_request_and_checks(monkeypatch):
    f, sd = _conflict_file()
    out = ai.ConflictAdvice(spots=[
        ai.SpotAdvice(spot=1, mine_does="Sets 3.", incoming_does="Sets 2.", pick=" Incoming ",
                      combined="", reason="Newer.", unsure=True),
        ai.SpotAdvice(spot=2, mine_does="Calls a.", incoming_does="Calls b.", pick="combined",
                      combined="<<<<<<< HEAD\na()\n", reason="Both.", unsure=False),
        ai.SpotAdvice(spot=3, mine_does="?", incoming_does="?", pick="mine", combined="", reason="", unsure=False),
    ])
    msgs = fake_client(monkeypatch, out)
    advice, tel = ai.advise_conflict(f, sd, (["main change"], ["login change"]))
    kw = msgs.calls[0]
    content = kw["messages"][0]["content"]
    assert kw["system"] == ai.CONFLICT_SYSTEM and kw["output_format"] is ai.ConflictAdvice
    assert kw["output_config"] == {"effort": "medium"}
    assert '<spot n="1" line="4">' in content and "def go():" in content and "- login change" in content
    assert 'n="3"' not in content  # too big: not sent, and its advice ignored
    assert set(advice) == {1, 2}
    assert advice[1].pick == "incoming" and advice[1].unsure and advice[1].mine_does == "Sets 3."
    assert advice[2].pick is None and advice[2].incoming_does == "Calls b."  # markers left in: no pick
    assert tel["spots"] == 2 and tel["picks"] == ["incoming", None]


def test_conflict_advice_whole_file_allows_only_one_side(monkeypatch):
    from gitly import conflicts
    f = conflicts.ConflictFile("d.txt", "DU", [conflicts.Spot("d.txt", 1, None, "edited\n", whole_file=True)])
    out = ai.ConflictAdvice(spots=[ai.SpotAdvice(spot=1, mine_does="Deletes it.", incoming_does="Edits it.",
                                                 pick="both_mine_first", combined="", reason="", unsure=False)])
    msgs = fake_client(monkeypatch, out)
    advice, _ = ai.advise_conflict(f, conflicts.Sides("main", "login", "merge"), ([], []))
    assert advice[1].pick is None
    content = msgs.calls[0]["messages"][0]["content"]
    assert 'whole_file="true"' in content and '<yours deleted="true"/>' in content


def test_conflict_advice_respects_opt_out(monkeypatch):
    monkeypatch.setenv("GITLY_AI_SEND_CODE", "0")
    msgs = fake_client(monkeypatch, None)
    f, sd = _conflict_file()
    with pytest.raises(ai.AIUnavailable):
        ai.advise_conflict(f, sd, ([], []))
    assert msgs.calls == []


def test_conflict_schema_is_accepted_by_sdk():
    from anthropic.lib._parse._transform import transform_schema
    assert transform_schema(ai.ConflictAdvice.model_json_schema())["additionalProperties"] is False
