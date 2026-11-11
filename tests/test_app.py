"""Drive the real TUI with Textual's pilot against throwaway repos."""

import json

from gitly import git
from gitly.app import AskTextScreen, Gitly, PlanScreen, ProblemScreen

from conftest import sh


async def choose(pilot, key):
    app = pilot.app
    menu = app.query_one("#menu")
    menu.highlighted = menu.get_option_index(key)
    await pilot.press("enter")
    await pilot.pause()


async def test_save_flow(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GITLY_LOG_DIR", str(tmp_path / "logs"))
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo), participant="P01")
    async with app.run_test(size=(120, 40)) as pilot:
        assert "1 file changed" in str(app.query_one("#summary").render())
        await choose(pilot, "save")
        assert isinstance(app.screen, AskTextScreen)
        await pilot.press(*"fix typo", "enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("enter")  # "Run it" is focused
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.repo.dirty
    assert "fix typo" in sh(repo, "log", "-1", "--format=%s")
    events = [json.loads(l)["event"] for l in next((tmp_path / "logs").iterdir()).read_text().splitlines()]
    assert events[:2] == ["start", "menu"] and "step" in events


async def test_cancel_runs_nothing(repo):
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "save")
        await pilot.press(*"msg", "enter")
        await pilot.pause()
        await pilot.press("escape")
        await app.workers.wait_for_complete()
    assert sh(repo, "log", "--format=%s").splitlines() == ["first"]


async def test_risky_needs_yes_and_backs_up(repo):
    (repo / "a.txt").write_text("precious\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "undo")
        ol = app.screen.query_one("OptionList")
        ol.highlighted = ol.get_option_index("discard")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("enter")  # empty confirm box: must not run
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press(*"yes", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert (repo / "a.txt").read_text() == "one\n"
    [b] = git.backups(repo)
    assert b.wip  # the discarded change is recoverable


async def test_push_rejected_rescue(remote_pair):
    repo, other = remote_pair
    (other / "o.txt").write_text("o"); sh(other, "add", "."); sh(other, "commit", "-qm", "o"); sh(other, "push", "-q")
    (repo / "r.txt").write_text("r"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "r")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "upload")
        await pilot.press("enter")  # run push
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, ProblemScreen):
                break
        assert isinstance(app.screen, ProblemScreen)
        await pilot.press("enter")  # first option: the fix
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    sh(repo, "fetch", "-q")
    assert git.state(repo).ahead == 0 and git.state(repo).behind == 0


async def test_ai_plan_blocked_step_cannot_run(repo, monkeypatch):
    from gitly import ai
    from gitly.plan import Plan, Step

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "ask", lambda q, s, e=None: (
        Plan("Bad idea", [Step(["-c", "core.pager=evil", "log"], "x")], source="ai", note="hi"), {}))
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press(*"show log", "enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, PlanScreen):
                break
        assert isinstance(app.screen, PlanScreen)
        assert not app.screen.query("#run")
        await pilot.press("enter")  # only "Close" is available
        await app.workers.wait_for_complete()


async def test_not_a_repo_offers_init(tmp_path):
    app = Gitly(str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "init")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert (tmp_path / ".git").is_dir()


async def test_save_prefills_ai_message(repo, tmp_path, monkeypatch):
    from gitly import ai

    monkeypatch.setenv("GITLY_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "suggest_commit_message", lambda s: ("Update a.txt", {"model": "m"}))
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo), participant="P02")
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "save")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, AskTextScreen):
                break
        assert app.screen.query_one("Input").value == "Update a.txt"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert sh(repo, "log", "-1", "--format=%s").strip() == "Update a.txt"
    lines = [json.loads(l) for l in next((tmp_path / "logs").iterdir()).read_text().splitlines()]
    assert {"event": "ai_commit_message_choice", "choice": "kept"}.items() <= next(
        e for e in lines if e["event"] == "ai_commit_message_choice").items()


async def test_save_falls_back_when_ai_fails(repo, monkeypatch):
    from gitly import ai

    def boom(s):
        raise ai.AIUnavailable("Couldn't reach the AI service.")

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "suggest_commit_message", boom)
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "save")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, AskTextScreen):
                break
        assert isinstance(app.screen, AskTextScreen)
        assert app.screen.query_one("Input").value == ""


async def test_explain_changes(repo, monkeypatch):
    from gitly import ai
    from gitly.app import TextScreen

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "summarize_changes", lambda s: (
        ai.ChangeSummary(overview="You rewrote a.txt.", points=["Replaced [one] with edited"]), {}))
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "explain")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, TextScreen):
                break
        assert isinstance(app.screen, TextScreen)
        text = str(app.screen.query("Static")[1].render())
        assert "You rewrote a.txt." in text and "[one]" in text  # markup escaped, not eaten


async def test_explain_hidden_when_clean(repo):
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        menu = app.query_one("#menu")
        ids = [menu.get_option_at_index(i).id for i in range(menu.option_count)]
        assert "explain" not in ids and "save" in ids


async def test_precheck_leaves_out_secrets(repo):
    from gitly.app import PrecheckScreen

    (repo / "a.txt").write_text("edited\n")
    (repo / ".env").write_text("A=1\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "save")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, PrecheckScreen):
                break
        assert isinstance(app.screen, PrecheckScreen)
        await pilot.press("enter")  # "Leave these out" is first
        await pilot.pause()
        assert isinstance(app.screen, AskTextScreen)
        await pilot.press(*"edit a", "enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        assert "add to .gitignore: .env" in str(app.screen.query(".step").first().render())
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert sorted(sh(repo, "show", "--name-only", "--format=", "HEAD").split()) == [".gitignore", "a.txt"]


async def test_precheck_cancel_saves_nothing(repo):
    from gitly.app import PrecheckScreen

    (repo / ".env").write_text("A=1\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "save")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, PrecheckScreen):
                break
        await pilot.press("escape")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert app.screen is app.screen_stack[0]
    assert sh(repo, "log", "--format=%s").splitlines() == ["first"]
    assert not (repo / ".gitignore").exists()
