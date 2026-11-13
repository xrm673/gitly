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
    assert events[:3] == ["start", "suggestion_shown", "menu"] and "step" in events


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
        assert app.screen.text == "Update a.txt"
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
        assert app.screen.text == ""


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


async def test_branch_map_on_home_screen(repo):
    from gitly.app import BranchMap

    sh(repo, "switch", "-qc", "login")
    (repo / "l.txt").write_text("x"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "login form")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bm = app.query_one(BranchMap)
        text = str(bm.query_one("#map").render())
        assert "login" in text and "you are here" in text  # visible without opening anything
        assert app.focused.id == "menu" and "←" in str(bm.query_one("#map-detail").render())
        await pilot.press("left")
        assert app.focused is bm and "login form" in str(bm.query_one("#map-detail").render())
        await pilot.press("escape")
        assert app.focused.id == "menu"
        menu = app.query_one("#menu")
        assert "map" not in [menu.get_option_at_index(i).id for i in range(menu.option_count)]


async def test_branch_map_keeps_selection_on_refresh(repo):
    from gitly.app import BranchMap

    (repo / "b.txt").write_text("b"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "second")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        bm = await _open_map(pilot)
        await pilot.press("left")
        picked = bm.selected()[1]
        built = bm.g
        app.refresh_state(keep_highlight=True)  # nothing moved: no rebuild
        assert bm.g is built and bm.selected()[1] == picked
        sh(repo, "branch", "other")  # a branch appears: rebuild, selection stays put
        app.refresh_state(keep_highlight=True)
        assert bm.g is not built and bm.selected()[1] == picked


async def test_arrows_move_between_map_and_menu(repo):
    (repo / "b.txt").write_text("b"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "second")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        bm = await _open_map(pilot)
        menu = app.query_one("#menu")
        await pilot.press("left")  # an older save: → walks back to the newest, stays on the map
        await pilot.press("right")
        assert app.focused is bm
        await pilot.press("right")  # past the newest save: over to the menu
        assert app.focused is menu
        await pilot.press("left")
        assert app.focused is bm


async def test_tab_takes_next_step_from_anywhere(repo):
    (repo / "a.txt").write_text("edited\n")  # unsaved work: the next step is to save it
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        assert app.suggestion.kind == "save" and app.query_one("#next-key").display
        await pilot.press("tab")
        dialog = await wait_for(pilot, AskTextScreen)
        await pilot.press("tab")  # inside a dialog, Tab only moves focus
        await pilot.pause()
        assert app.screen is dialog and len(app.screen_stack) == 2


async def wait_for(pilot, cls, tries=60):
    for _ in range(tries):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, cls):
            return pilot.app.screen
    raise AssertionError(f"never reached {cls.__name__}, at {type(pilot.app.screen).__name__}")


async def test_map_new_branch_from_older_save(repo):
    (repo / "b.txt").write_text("b"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "second")
    first = sh(repo, "rev-parse", "HEAD~1").strip()
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_map(pilot)
        assert screen.selected()[1] == sh(repo, "rev-parse", "HEAD").strip()  # starts at HEAD
        await pilot.press("left")
        assert screen.selected()[1] == first
        assert "“first”" in str(screen.query_one("#map-detail").render())
        await pilot.press("n")
        await wait_for(pilot, AskTextScreen)
        await pilot.press(*"old idea", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args == ["switch", "--create", "old-idea", first[:12]]
        preview = str(plan_screen.query_one(".preview").render())
        assert "Now" in preview and "After" in preview and "new branch" in preview
        await pilot.press("enter")
        await _wait_home(pilot)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "old-idea" in str(screen.query_one("#map").render())
    assert sh(repo, "branch", "--show-current").strip() == "old-idea"
    assert sh(repo, "rev-parse", "HEAD").strip() == first


async def test_map_switch_carries_unsaved_changes(repo):
    sh(repo, "switch", "-qc", "login")
    (repo / "l.txt").write_text("l"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "login form")
    sh(repo, "switch", "-q", "main")
    (repo / "notes.txt").write_text("unsaved\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_map(pilot)
        await pilot.press("down", "down")
        assert screen.selected()[0].name == "login"
        await pilot.press("enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.args for st in plan_screen.plan.steps] == [["switch", "login"]]
        assert "comes with you" in plan_screen.plan.steps[0].why
        await pilot.press("enter")
        await _wait_home(pilot)
        await app.workers.wait_for_complete()
    assert sh(repo, "branch", "--show-current").strip() == "login"
    assert (repo / "notes.txt").read_text() == "unsaved\n"


async def test_map_switch_saves_first_when_in_the_way(repo):
    from gitly.app import ChooseScreen

    sh(repo, "switch", "-qc", "login")
    (repo / "a.txt").write_text("login version\n"); sh(repo, "commit", "-qam", "login edit")
    sh(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("my unsaved edit\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("down", "down", "enter")
        choose_screen = await wait_for(pilot, ChooseScreen)
        assert "a.txt" in str(choose_screen.query("Static")[1].render())
        await pilot.press("enter")  # save first
        await wait_for(pilot, AskTextScreen)
        await pilot.press(*"my edit", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.args[0] for st in plan_screen.plan.steps] == ["add", "commit", "switch"]
        await pilot.press("enter")
        await _wait_home(pilot)
        await app.workers.wait_for_complete()
    assert sh(repo, "branch", "--show-current").strip() == "login"
    assert sh(repo, "log", "-1", "--format=%s", "main").strip() == "my edit"
    assert sh(repo, "show", "main:a.txt") == "my unsaved edit\n"


async def test_map_rejects_bad_names_and_remote_rows(repo):
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("enter")  # already on main
        await pilot.pause()
        await pilot.press("n")
        await wait_for(pilot, AskTextScreen)
        await pilot.press(*"main", "enter")  # exists
        await _wait_home(pilot)
        await app.workers.wait_for_complete()
    assert sh(repo, "branch", "--format=%(refname:short)").split() == ["main"]


def _login_branch(repo, path="l.txt", text="l\n"):
    sh(repo, "switch", "-qc", "login")
    (repo / path).write_text(text); sh(repo, "add", "."); sh(repo, "commit", "-qm", "login form")
    sh(repo, "switch", "-q", "main")


async def _open_map(pilot):
    """Focus the branch map panel on the home screen."""
    from gitly.app import BranchMap
    await pilot.pause()
    await pilot.press("left")
    bm = pilot.app.query_one(BranchMap)
    assert pilot.app.focused is bm
    return bm


async def _wait_home(pilot, tries=60):
    for _ in range(tries):
        await pilot.pause(0.05)
        if pilot.app.screen is pilot.app.screen_stack[0]:
            return
    raise AssertionError(f"still on {type(pilot.app.screen).__name__}")


async def _run_plan_and_return(pilot):
    await pilot.press("enter")
    await _wait_home(pilot)
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def test_map_combine_branch(repo):
    _login_branch(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_map(pilot)
        await pilot.press("down", "down", "space")
        assert screen.held == "login"
        assert "Carrying login" in str(screen.query_one("#map-detail").render())
        await pilot.press("up", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.args for st in plan_screen.plan.steps] == [["merge", "--no-ff", "--no-edit", "login"]]
        assert "No conflicts expected" in plan_screen.plan.note
        assert "new combined save" in str(plan_screen.query_one(".preview").render())
        await _run_plan_and_return(pilot)
        assert screen.held is None
    assert sh(repo, "log", "-1", "--format=%P", "main").count(" ") == 1  # a merge commit
    assert (repo / "l.txt").exists()


async def test_map_combine_from_other_branch_switches_first(repo):
    _login_branch(repo)
    sh(repo, "switch", "-q", "login")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_map(pilot)
        assert screen.selected()[0].name == "login"
        await pilot.press("space", "up", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.args[0] for st in plan_screen.plan.steps] == ["switch", "merge"]
        await _run_plan_and_return(pilot)
    assert sh(repo, "branch", "--show-current").strip() == "main"


async def test_map_combine_predicts_conflict(repo):
    _login_branch(repo, "a.txt", "login version\n")
    (repo / "a.txt").write_text("main version\n"); sh(repo, "commit", "-qam", "main edit")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("down", "down", "space", "up", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert "a.txt" in plan_screen.plan.note and "Both branches changed" in plan_screen.plan.note
        await pilot.press("escape")
        await app.workers.wait_for_complete()
    assert sh(repo, "status", "--porcelain") == ""


async def test_map_esc_puts_branch_down(repo):
    _login_branch(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open_map(pilot)
        await pilot.press("down", "down", "space", "escape")
        await pilot.pause()
        assert screen.held is None and app.focused is screen  # first Esc puts it down
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused.id == "menu"


async def test_map_undo_latest_unpushed_save(repo):
    from gitly.app import ChooseScreen
    (repo / "b.txt").write_text("b\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "add b")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("u")
        await wait_for(pilot, ChooseScreen)
        await pilot.press("enter")  # un-save (recommended)
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.args for st in plan_screen.plan.steps] == [["reset", "--soft", "HEAD~1"]]
        assert "1 unsaved file" in str(plan_screen.query_one(".preview").render())
        await _run_plan_and_return(pilot)
    assert sh(repo, "log", "--format=%s").splitlines() == ["first"]
    assert (repo / "b.txt").read_text() == "b\n"  # nothing lost


async def test_map_undo_merge_uses_revert_m1(repo):
    _login_branch(repo)
    sh(repo, "merge", "-q", "--no-ff", "--no-edit", "login")
    (repo / "z.txt").write_text("unrelated\n"); sh(repo, "add", "z.txt")  # staged, unrelated
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("u")  # HEAD is the merge: no choice, straight to the plan
        plan_screen = await wait_for(pilot, PlanScreen)
        steps = [st.args for st in plan_screen.plan.steps]
        assert steps[0] == ["restore", "--staged", "."]
        assert steps[1][:4] == ["revert", "--no-edit", "-m", "1"]
        assert "merging the same branch again" in plan_screen.plan.note
        await _run_plan_and_return(pilot)
    assert not (repo / "l.txt").exists()
    assert sh(repo, "log", "-1", "--format=%s").startswith("Revert")
    assert (repo / "z.txt").read_text() == "unrelated\n"


async def test_map_undo_older_save_reverts(repo):
    (repo / "b.txt").write_text("b\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "add b")
    (repo / "c.txt").write_text("c\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "add c")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("left", "u")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args[:2] == ["revert", "--no-edit"]
        assert "Later saves stay as they are" in plan_screen.plan.note
        await _run_plan_and_return(pilot)
    assert not (repo / "b.txt").exists() and (repo / "c.txt").exists()


async def test_map_undo_refuses_other_branch(repo):
    _login_branch(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_map(pilot)
        await pilot.press("down", "down", "u")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert not isinstance(app.screen, PlanScreen)
    assert sh(repo, "log", "-1", "--format=%s", "login").strip() == "login form"


async def _new_branch_via_menu(pilot):
    from gitly.app import ChooseScreen
    await choose(pilot, "branches")
    await wait_for(pilot, ChooseScreen)
    ol = pilot.app.screen.query_one("OptionList")
    ol.highlighted = ol.get_option_index("new")
    await pilot.press("enter")
    await wait_for(pilot, AskTextScreen)


async def test_ai_tidies_branch_name(repo, tmp_path, monkeypatch):
    from gitly import ai
    from gitly.app import ChooseScreen
    monkeypatch.setenv("GITLY_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(ai, "available", lambda: True)
    seen = {}

    def suggest(typed, s, start, existing):
        seen.update(typed=typed, start=start, existing=existing)
        return ai.BranchName(name="fix-mobile-footer", reason="made it more specific"), {}

    monkeypatch.setattr(ai, "suggest_branch_name", suggest)
    app = Gitly(str(repo), participant="P03")
    async with app.run_test(size=(120, 40)) as pilot:
        await _new_branch_via_menu(pilot)
        await pilot.press(*"footer thing", "enter")
        pick = await wait_for(pilot, ChooseScreen)
        assert [o[0] for o in pick.options] == ["ai", "mine", "edit"]
        assert "footer thing" in str(pick.query(".muted").first().render())
        await pilot.press("enter")  # the AI's name is first
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args == ["switch", "--create", "fix-mobile-footer"]
        await pilot.press("enter")
        await app.workers.wait_for_complete()
    assert seen == {"typed": "footer thing", "start": "where you are", "existing": ["main"]}
    assert sh(repo, "branch", "--show-current").strip() == "fix-mobile-footer"
    lines = [json.loads(l) for l in next((tmp_path / "logs").iterdir()).read_text().splitlines()]
    assert any(e["event"] == "ai_branch_name_choice" and e["choice"] == "kept" for e in lines)


async def test_ai_branch_name_can_be_turned_down(repo, tmp_path, monkeypatch):
    from gitly import ai
    from gitly.app import ChooseScreen
    monkeypatch.setenv("GITLY_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "suggest_branch_name",
                        lambda *a: (ai.BranchName(name="fix-mobile-footer", reason="more specific"), {}))
    app = Gitly(str(repo), participant="P04")
    async with app.run_test(size=(120, 40)) as pilot:
        await _new_branch_via_menu(pilot)
        await pilot.press(*"Footer Thing", "enter")
        await wait_for(pilot, ChooseScreen)
        await pilot.press("down", "enter")  # "footer-thing, the name you typed"
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args == ["switch", "--create", "footer-thing"]
    lines = [json.loads(l) for l in next((tmp_path / "logs").iterdir()).read_text().splitlines()]
    assert any(e["event"] == "ai_branch_name_choice" and e["choice"] == "mine" for e in lines)


async def test_ai_branch_name_same_as_typed_skips_second_prompt(repo, monkeypatch):
    from gitly import ai
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "suggest_branch_name",
                        lambda *a: (ai.BranchName(name="dark-mode", reason="kept as is"), {}))
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _new_branch_via_menu(pilot)
        await pilot.press(*"Dark mode", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args == ["switch", "--create", "dark-mode"]


async def test_ai_branch_name_falls_back(repo, monkeypatch):
    from gitly import ai

    def boom(*a):
        raise ai.AIUnavailable("Couldn't reach the AI service.")

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "suggest_branch_name", boom)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _new_branch_via_menu(pilot)
        await pilot.press(*"Login stuff!", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args == ["switch", "--create", "login-stuff"]


async def test_ai_branch_name_never_suggests_existing(repo, monkeypatch):
    from gitly import ai
    sh(repo, "branch", "login-page")
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "suggest_branch_name",
                        lambda *a: (ai.BranchName(name="login-page", reason="more specific"), {}))
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await _new_branch_via_menu(pilot)
        await pilot.press(*"login", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert plan_screen.plan.steps[0].args == ["switch", "--create", "login"]  # kept the user's


async def test_ask_without_key_offers_to_save_one(repo, tmp_path, monkeypatch):
    from gitly import ai

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, AskTextScreen):
                break
        assert app.screen.title_text == "Turn on AI"
        assert app.screen.query_one("Input").password
        await pilot.press(*"sk-ant-abc", "enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if isinstance(app.screen, AskTextScreen) and app.screen.title_text == "Ask in plain English":
                break
        assert app.screen.title_text == "Ask in plain English"  # goes straight on to the question
        await pilot.press("escape")
        await app.workers.wait_for_complete()
    assert ai.saved_key() == "sk-ant-abc"


# ---- the next step

def text_of(app, selector):
    return str(app.query_one(selector).render())


def menu_ids(app):
    menu = app.query_one("#menu")
    return [o.id for o in (menu.get_option_at_index(i) for i in range(menu.option_count))]


async def test_home_starts_on_the_next_step(repo):
    from gitly.app import BranchMap

    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "Save your work" in text_of(app, "#next-title") and "1 file" in text_of(app, "#next-reason")
        assert app.query_one("#menu").highlighted_option.id == "next"
        assert "◉╌╌◌" in str(app.query_one(BranchMap).query_one("#map").render())  # drawn on the map too
        await pilot.press("enter")
        await wait_for(pilot, AskTextScreen)
        await pilot.press(*"typo", "enter")
        await wait_for(pilot, PlanScreen)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Saved: the suggestion moves on, and so does the cursor.
        assert "Connect to GitHub" in text_of(app, "#next-title")
        assert app.query_one("#menu").highlighted_option.id == "next"
        assert "◌" not in str(app.query_one(BranchMap).query_one("#map").render())
    assert sh(repo, "log", "-1", "--format=%s").strip() == "typo"


async def test_nothing_to_save_is_greyed_out(remote_pair):
    repo, _ = remote_pair
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        menu = app.query_one("#menu")
        assert "all caught up" in text_of(app, "#next-title") and "next" not in menu_ids(app)
        assert not app.query_one("#next-key").display  # no Tab key cap without a step to take
        assert menu.get_option("save").disabled and menu.get_option("upload").disabled
        assert "nothing to upload" in str(menu.get_option("upload").prompt)
        assert menu.highlighted_option.id == "pull"
        (repo / "a.txt").write_text("edited\n")
        app.refresh_state(keep_highlight=True)
        assert not menu.get_option("save").disabled and menu_ids(app)[0] == "next"


async def test_skip_suggestion(repo, tmp_path, monkeypatch):
    from gitly.app import BranchMap

    monkeypatch.setenv("GITLY_LOG_DIR", str(tmp_path / "logs"))
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo), participant="P09")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert "Nothing suggested" in text_of(app, "#next-title") and "next" not in menu_ids(app)
        assert "◌" not in str(app.query_one(BranchMap).query_one("#map").render())
        app.refresh_state(keep_highlight=True)  # it stays skipped
        assert "next" not in menu_ids(app)
    events = [json.loads(l) for l in next((tmp_path / "logs").iterdir()).read_text().splitlines()]
    assert {"event": "suggestion_dismissed", "step": "save"}.items() <= next(
        e for e in events if e["event"] == "suggestion_dismissed").items()


async def test_accepting_combine_shows_the_plan(repo):
    sh(repo, "switch", "-qc", "login")
    (repo / "l.txt").write_text("x"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "login form")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "Combine login into main" in text_of(app, "#next-title")
        await pilot.press("enter")
        plan = (await wait_for(pilot, PlanScreen)).plan
        assert [st.args[0] for st in plan.steps] == ["switch", "merge"] and plan.preview
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Delete login" in text_of(app, "#next-title")  # combined, so the branch can go
    assert "login form" in sh(repo, "log", "main", "--format=%s")


def ai_on(monkeypatch, reply):
    """Turn on background suggestions with a canned AI answer. Returns the list of calls made."""
    from gitly import ai, suggest

    calls = []

    def fake(s, g, base):
        calls.append(base.kind)
        if isinstance(reply, Exception):
            raise reply
        return reply(s, base), {"model": "m"}

    monkeypatch.setenv("GITLY_AI_SUGGEST", "1")
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(suggest, "from_ai", fake)
    return calls


async def test_ai_splits_unrelated_changes(repo, monkeypatch):
    from gitly import suggest
    from gitly.app import BranchMap

    calls = ai_on(monkeypatch, lambda s, base: suggest.Suggestion(
        "split_save", "Save as 2 separate saves", "Two unrelated things changed.", source="ai",
        groups=[suggest.SaveGroup("Fix a", ["a.txt"]), suggest.SaveGroup("Add payments", ["pay.py"])]))
    (repo / "a.txt").write_text("edited\n")
    (repo / "pay.py").write_text("x")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Save as 2 separate saves" in text_of(app, "#next-title")
        assert "◉╌╌◌╌╌◌" in str(app.query_one(BranchMap).query_one("#map").render())
        app.refresh_state(keep_highlight=True)
        await app.workers.wait_for_complete()
        assert calls == ["save"]  # same files, same answer: the AI isn't asked again
        await pilot.press("enter")
        plan = (await wait_for(pilot, PlanScreen)).plan
        assert plan.source == "ai" and [st.args[0] for st in plan.steps] == ["add", "commit", "add", "commit"]
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.repo.dirty
    assert sh(repo, "log", "--format=%s").splitlines() == ["Add payments", "Fix a", "first"]


async def test_ai_branch_for_unrelated_work_starts_from_main(repo, monkeypatch):
    from gitly import suggest
    from gitly.app import BranchMap

    ai_on(monkeypatch, lambda s, base: suggest.Suggestion(
        "new_branch", "Start a branch for this: add-hikes (from main)", "Not about dark mode.",
        source="ai", branch="add-hikes", target="main"))
    sh(repo, "switch", "-qc", "dark-mode")
    (repo / "theme.css").write_text("dark"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "Add dark mode")
    (repo / "hikes.js").write_text("hikes")
    main = sh(repo, "rev-parse", "main").strip()
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "add-hikes" in text_of(app, "#next-title")
        rows = str(app.query_one(BranchMap).query_one("#map").render()).splitlines()
        assert "add-hikes" in rows[1] and "dark-mode" in rows[2]  # drawn under main
        await pilot.press("enter")
        plan = (await wait_for(pilot, PlanScreen)).plan
        assert plan.steps[-1].args == ["switch", "--create", "add-hikes", main[:12]]
        assert "'main'" in plan.steps[-1].why
        await _run_plan_and_return(pilot)
    assert sh(repo, "branch", "--show-current").strip() == "add-hikes"
    assert sh(repo, "rev-parse", "HEAD").strip() == main  # dark mode's save didn't come along
    assert (repo / "hikes.js").read_text() == "hikes" and not (repo / "theme.css").exists()


async def test_ai_split_gives_way_to_the_pre_save_check(repo, monkeypatch):
    from gitly import suggest
    from gitly.app import PrecheckScreen

    ai_on(monkeypatch, lambda s, base: suggest.Suggestion(
        "split_save", "Save as 2 separate saves", "Two things.", source="ai",
        groups=[suggest.SaveGroup("Fix a", ["a.txt"]), suggest.SaveGroup("Add settings", [".env"])]))
    (repo / "a.txt").write_text("edited\n")
    (repo / ".env").write_text("SECRET=1\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await wait_for(pilot, PrecheckScreen)  # the secrets file is caught, not saved in a group


async def test_ai_description_is_reused_by_save(repo, monkeypatch):
    from gitly import ai, suggest

    ai_on(monkeypatch, lambda s, base: suggest.Suggestion(
        "save", "Save your work: “Rewrite a.txt”", "One change.", source="ai", message="Rewrite a.txt"))

    def never(s):
        raise AssertionError("the description was already written")

    monkeypatch.setattr(ai, "suggest_commit_message", never)
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await choose(pilot, "save")  # the plain menu item, not the suggestion
        assert (await wait_for(pilot, AskTextScreen)).text == "Rewrite a.txt"


async def test_ai_trouble_stays_out_of_the_way(repo, monkeypatch):
    from gitly import ai

    calls = ai_on(monkeypatch, ai.AIUnavailable("Couldn't reach the AI service."))
    (repo / "a.txt").write_text("edited\n")
    app = Gitly(str(repo))
    app.AI_GAP = 0
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is app.screen_stack[0]  # no dialog
        assert text_of(app, "#next-title").strip().endswith("Save your work")
        for _ in range(3):
            app.refresh_state(keep_highlight=True)
            await app.workers.wait_for_complete()
        assert calls == ["save"]  # not retried for the same files


async def test_ai_waits_for_files_to_settle(repo, monkeypatch):
    from gitly import suggest

    calls = ai_on(monkeypatch, lambda s, base: suggest.Suggestion(
        "save", "Save your work: “x”", "One change.", source="ai", message="x"))
    app = Gitly(str(repo))  # clean, local only: nothing for the AI to weigh in on
    app.AI_GAP = 0
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        assert calls == []
        (repo / "a.txt").write_text("being edited\n")
        app.refresh_state(keep_highlight=True, settle=True)  # what the 3-second poll does
        await app.workers.wait_for_complete()
        assert calls == []  # just changed: might be mid-edit
        app.AI_SETTLE = 0
        app.refresh_state(keep_highlight=True, settle=True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["save"] and "“x”" in text_of(app, "#next-title")


def _conflicted_merge(repo):
    _login_branch(repo, "a.txt", "login version\n")
    (repo / "a.txt").write_text("main version\n"); sh(repo, "commit", "-qam", "main edit")
    assert not git.run(["merge", "login"], repo).ok


async def test_finish_merge_stops_on_leftover_markers(repo):
    from gitly.app import ChooseScreen

    _conflicted_merge(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "finish_merge")
        warn = await wait_for(pilot, ChooseScreen)
        assert "Conflict markers" in warn.title_text and "a.txt" in warn.hint
        await pilot.press("enter")  # "Go back and fix them" is first
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
    assert git.state(repo).merging and "<<<<<<<" in (repo / "a.txt").read_text()


async def test_finish_merge_after_fixing_markers(repo):
    _conflicted_merge(repo)
    (repo / "a.txt").write_text("both versions\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "finish_merge")
        await wait_for(pilot, PlanScreen)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert not git.state(repo).merging
    assert sh(repo, "log", "-1", "--format=%P").count(" ") == 1


async def test_finish_rebase_continues_it(repo):
    _login_branch(repo, "a.txt", "login version\n")
    sh(repo, "switch", "-q", "login")
    (repo / "b.txt").write_text("b\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "login second")
    sh(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("main version\n"); sh(repo, "commit", "-qam", "main edit")
    sh(repo, "switch", "-q", "login")
    assert not git.run(["rebase", "main"], repo).ok
    (repo / "a.txt").write_text("both versions\n")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        menu = app.query_one("#menu")
        ids = [menu.get_option_at_index(i).id for i in range(menu.option_count)]
        assert "finish_merge" in ids and "branch_here" not in ids
        await choose(pilot, "finish_merge")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.args for st in plan_screen.plan.steps] == [["add", "--all"], ["rebase", "--continue"]]
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    s = git.state(repo)
    assert not s.rebasing and s.branch == "login"
    assert sh(repo, "log", "--format=%s").splitlines() == ["login second", "login form", "main edit", "first"]


async def test_background_fetch_shows_new_commits(remote_pair, monkeypatch):
    monkeypatch.setenv("GITLY_AUTO_FETCH", "1")
    repo, other = remote_pair
    (other / "o.txt").write_text("o"); sh(other, "add", "."); sh(other, "commit", "-qm", "o"); sh(other, "push", "-q")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.repo.behind == 1
        summary = str(app.query_one("#summary").render())
        assert "GitHub has 1 commit you don't" in summary and "checked GitHub just now" in summary
        assert app.suggestion.kind == "pull"
    assert sh(repo, "log", "--format=%s").splitlines() == ["first"]  # fetch only, nothing merged


async def test_background_fetch_failure_is_quiet(repo, monkeypatch):
    monkeypatch.setenv("GITLY_AUTO_FETCH", "1")
    sh(repo, "remote", "add", "origin", str(repo.parent / "missing.git"))
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "couldn't reach GitHub just now" in str(app.query_one("#summary").render())
        assert app.screen is app.screen_stack[0]  # no error dialog


def _push_new_branch(repo, other, name="login"):
    sh(other, "switch", "-qc", name)
    (other / "l.txt").write_text("l\n"); sh(other, "add", "."); sh(other, "commit", "-qm", "login form")
    sh(other, "push", "-qu", "origin", name)
    sh(repo, "fetch", "-q")


async def test_switch_list_offers_github_only_branch(remote_pair):
    repo, other = remote_pair
    _push_new_branch(repo, other)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(pilot, "branches")
        ol = app.screen.query_one("OptionList")
        ol.highlighted = ol.get_option_index("switch")
        await pilot.press("enter")
        await pilot.pause()
        ol = app.screen.query_one("OptionList")
        assert "only on GitHub" in str(ol.get_option("github:login").prompt)
        ol.highlighted = ol.get_option_index("github:login")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        assert app.screen.plan.steps[0].args == ["switch", "--track", "origin/login"]
        await _run_plan_and_return(pilot)
    assert sh(repo, "branch", "--show-current").strip() == "login"
    assert sh(repo, "rev-parse", "--abbrev-ref", "login@{upstream}").strip() == "origin/login"


async def test_map_enter_gets_github_only_branch(remote_pair):
    repo, other = remote_pair
    _push_new_branch(repo, other)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        bm = await _open_map(pilot)
        row = next(r for r, l in enumerate(bm.g.lanes) if l.kind == "github")
        bm.sel = (row, 0)
        await pilot.press("space")  # can't carry it: it isn't here yet
        await pilot.pause()
        assert bm.held is None
        await pilot.press("enter")
        await wait_for(pilot, PlanScreen)
        assert app.screen.plan.steps[-1].args == ["switch", "--track", "origin/login"]
        await _run_plan_and_return(pilot)
    assert sh(repo, "branch", "--show-current").strip() == "login"


async def test_map_up_down_lands_on_latest_save(repo):
    sh(repo, "switch", "-qc", "login")
    for n in ("one", "two", "three"):
        (repo / f"{n}.txt").write_text(n); sh(repo, "add", "."); sh(repo, "commit", "-qm", n)
    sh(repo, "switch", "-q", "main")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        bm = await _open_map(pilot)
        await pilot.press("down", "down")
        lane, sha = bm.selected()
        assert lane.name == "login" and sha == lane.tip
        await pilot.press("left", "up", "down")
        assert bm.selected()[1] == lane.tip


async def test_map_new_branch_row_on_main(repo):
    _login_branch(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        bm = await _open_map(pilot)
        assert "+ new branch" in str(bm.query_one("#map").render())
        await pilot.press("down")  # main → the new-branch row right under it
        assert bm.on_new and "New branch" in str(bm.query_one("#map-detail").render())
        await pilot.press("down")  # → login
        assert not bm.on_new and bm.selected()[0].name == "login"
        await pilot.press("up", "up")  # back over the new row to main
        assert not bm.on_new and bm.selected()[0].name == "main"
        await pilot.press("down", "enter")
        await wait_for(pilot, AskTextScreen)
        await pilot.press(*"idea", "enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert ["switch", "--create", "idea"] in [st.args for st in plan_screen.plan.steps]
        await _run_plan_and_return(pilot)
        assert "+ new branch" not in str(bm.query_one("#map").render())  # only shown on main
    assert sh(repo, "branch", "--show-current").strip() == "idea"


async def test_map_rows_are_clickable(repo):
    _login_branch(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bm = app.query_one("#branchmap")
        # Lines: main, + new branch, login.
        await pilot.click("#map-wrap", offset=(60, 2))  # login's note, past its dots
        assert app.focused is bm and not bm.on_new and bm.selected()[0].name == "login"
        await pilot.click("#map-wrap", offset=(1, 0))  # main's name
        assert bm.selected()[0].name == "main" and bm.selected()[1] == bm.selected()[0].tip
        first_col, first_sha = bm.cells[0][0]
        await pilot.click("#map-wrap", offset=(bm._label_w() + 3 * first_col, 0))  # main's oldest dot
        assert bm.selected() == (bm.g.lanes[0], first_sha)
        await pilot.click("#map-wrap", offset=(60, 1))  # "start a new branch from here"
        assert bm.on_new
        await pilot.click("#map-wrap", offset=(2, 1))  # clicking it again starts the branch
        await wait_for(pilot, AskTextScreen)


async def test_map_cursor_follows_a_new_save(repo):
    (repo / "b.txt").write_text("b"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "second")
    app = Gitly(str(repo))
    async with app.run_test(size=(120, 40)) as pilot:
        bm = await _open_map(pilot)
        assert bm.selected()[1] == bm.selected()[0].tip  # on main's latest save
        (repo / "c.txt").write_text("c"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "third")
        app.refresh_state(keep_highlight=True)
        lane, sha = bm.selected()
        assert lane.name == "main" and sha == sh(repo, "rev-parse", "HEAD").strip()
        await pilot.press("left")  # an older save, picked on purpose, stays picked
        picked = bm.selected()[1]
        (repo / "d.txt").write_text("d"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "fourth")
        app.refresh_state(keep_highlight=True)
        assert bm.selected()[1] == picked


def _fake_advice(monkeypatch, advice):
    from gitly import ai
    calls = []
    monkeypatch.setattr(ai, "available", lambda: True)

    def advise(f, sd, hist):
        calls.append((f.path, sd.incoming, hist))
        return advice, {"spots": len(advice)}
    monkeypatch.setattr(ai, "advise_conflict", advise)
    return calls


async def test_resolve_conflicts_with_ai_advice(repo, monkeypatch):
    from gitly import conflicts
    from gitly.app import ResolveScreen

    _conflicted_merge(repo)
    calls = _fake_advice(monkeypatch, {1: conflicts.Advice(
        "Says main.", "Says login.", conflicts.COMBINED, "main and login version\n", "Both matter.", False)})
    app = Gitly(str(repo))
    async with app.run_test(size=(140, 45)) as pilot:
        assert app.suggestion.menu == "resolve"
        await pilot.press("tab")
        screen = await wait_for(pilot, ResolveScreen)
        assert calls == [("a.txt", "login", (["main edit"], ["login form"]))]
        assert "AI suggests" in str(screen.query_one("#advice").render())
        assert "Says login." in str(screen.query_one("#incoming-does").render())
        assert screen.query_one("#combined").has_class("show")
        assert screen.query_one("#picks").highlighted_option.id == conflicts.COMBINED
        await pilot.press("enter")
        plan_screen = await wait_for(pilot, PlanScreen)
        assert [st.display for st in plan_screen.plan.steps] == ["write a.txt", "git add -- a.txt"]
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.suggestion.menu == "finish_merge"
    assert (repo / "a.txt").read_text() == "main and login version\n"
    assert git.state(repo).merging and not git.state(repo).conflicts


async def test_resolve_without_ai_still_works(repo, monkeypatch):
    from gitly import conflicts
    from gitly.app import ResolveScreen

    _conflicted_merge(repo)
    (repo / "b.txt").write_text("b\n")  # a second clashing file
    sh(repo, "merge", "--abort")
    sh(repo, "switch", "-q", "login"); (repo / "b.txt").write_text("login b\n")
    sh(repo, "add", "."); sh(repo, "commit", "-qm", "login b"); sh(repo, "switch", "-q", "main")
    (repo / "b.txt").write_text("main b\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "main b")
    assert not git.run(["merge", "login"], repo).ok
    app = Gitly(str(repo))
    async with app.run_test(size=(140, 45)) as pilot:
        app._resolve_asked_key = True  # don't stop to ask for an API key
        await choose(pilot, "resolve")
        screen = await wait_for(pilot, ResolveScreen)
        assert "the AI is off" in str(screen.query_one("#advice").render())
        assert "Last save here" in str(screen.query_one("#mine-does").render())
        await pilot.press("down", "enter")  # a.txt: keep incoming
        assert "b.txt" in str(screen.query_one("#where").render())
        await pilot.press("b")  # back to a.txt: the pick is remembered
        assert screen.query_one("#picks").highlighted_option.id == conflicts.INCOMING
        await pilot.press("enter", "enter")  # b.txt: keep yours
        plan_screen = await wait_for(pilot, PlanScreen)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
    assert (repo / "a.txt").read_text() == "login version\n"
    assert (repo / "b.txt").read_text() == "main b\n"


async def test_resolve_refuses_to_overwrite_newer_edits(repo, monkeypatch):
    from gitly.app import ResolveScreen

    _conflicted_merge(repo)
    app = Gitly(str(repo))
    async with app.run_test(size=(140, 45)) as pilot:
        app._resolve_asked_key = True
        await choose(pilot, "resolve")
        await wait_for(pilot, ResolveScreen)
        await pilot.press("enter")
        await wait_for(pilot, PlanScreen)
        (repo / "a.txt").write_text("edited in the editor meanwhile\n")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
    assert (repo / "a.txt").read_text() == "edited in the editor meanwhile\n"


async def test_pull_conflict_offers_sorting_out(remote_pair, monkeypatch):
    from gitly import conflicts
    from gitly.app import ResolveScreen

    repo, other = remote_pair
    (other / "a.txt").write_text("theirs\n"); sh(other, "commit", "-qam", "t"); sh(other, "push", "-q")
    (repo / "a.txt").write_text("mine\n"); sh(repo, "commit", "-qam", "m")
    sh(repo, "fetch", "-q")
    _fake_advice(monkeypatch, {1: conflicts.Advice("Mine.", "Theirs.", conflicts.MINE, "", "Yours is newer.", True)})
    app = Gitly(str(repo))
    async with app.run_test(size=(140, 45)) as pilot:
        await choose(pilot, "pull")
        await wait_for(pilot, PlanScreen)
        await pilot.press("enter")
        problem = await wait_for(pilot, ProblemScreen)
        assert problem.query_one("OptionList").get_option_at_index(0).id == "input"
        await pilot.press("enter")
        screen = await wait_for(pilot, ResolveScreen)
        assert "GitHub's version" in screen.query_one("#incoming").border_title
        assert "isn't sure" in str(screen.query_one("#advice").render())
