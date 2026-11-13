"""The next step: what the rules suggest, and what the AI is allowed to change."""

from gitly import actions, ai, git, graph, suggest

from conftest import sh


def look(repo):
    s = git.state(repo)
    return s, graph.build(s)


def commit(repo, name):
    (repo / f"{name}.txt").write_text(name)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", name)


def test_not_a_repo(tmp_path):
    sg = suggest.rules(None, None)
    assert (sg.kind, sg.menu) == ("menu", "init") and suggest.ghost(sg, None) is None


def test_unsaved_changes_come_first(remote_pair):
    repo, _ = remote_pair
    commit(repo, "not uploaded")
    (repo / "a.txt").write_text("edited\n")
    s, g = look(repo)
    sg = suggest.rules(s, g)
    assert sg.kind == "save" and "1 file" in sg.reason
    assert suggest.ghost(sg, s) == graph.Ghost(saves=1)


def test_upload_then_caught_up(remote_pair):
    repo, _ = remote_pair
    commit(repo, "mine")
    s, g = look(repo)
    sg = suggest.rules(s, g)
    assert (sg.kind, sg.menu) == ("upload", "upload") and suggest.ghost(sg, s).tails == {"main": "⇡ upload"}
    sh(repo, "push", "-q")
    sg = suggest.rules(*look(repo))
    assert sg.kind == "none" and not sg.acceptable


def test_get_latest_before_uploading(remote_pair):
    repo, other = remote_pair
    commit(other, "teammate")
    sh(other, "push", "-q")
    sh(repo, "fetch", "-q")
    commit(repo, "mine")
    sg = suggest.rules(*look(repo))
    assert (sg.kind, sg.menu) == ("pull", "pull")


def test_new_branch_is_uploaded_then_combined(remote_pair):
    repo, _ = remote_pair
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    assert suggest.rules(*look(repo)).kind == "upload"
    sh(repo, "push", "-qu", "origin", "login")
    s, g = look(repo)
    sg = suggest.rules(s, g)
    assert (sg.kind, sg.branch, sg.target) == ("merge", "login", "main")
    assert suggest.ghost(sg, s).merge == ("login", "main")
    assert suggest.allowed(s, sg) == ["combine", "nothing"]


def test_combined_branch_can_go(repo):
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    sh(repo, "switch", "-q", "main")
    sh(repo, "merge", "-q", "--no-ff", "--no-edit", "login")
    s, g = look(repo)
    sg = suggest.rules(s, g)
    assert (sg.kind, sg.branch) == ("cleanup", "login")
    assert suggest.ghost(sg, s).tails == {"login": "✕ delete"}


def test_local_only_project_is_pointed_at_github(repo):
    sg = suggest.rules(*look(repo))
    assert (sg.kind, sg.menu) == ("menu", "connect")


def test_conflicts_lead_to_sorting_them_out(repo):
    sh(repo, "switch", "-qc", "other")
    (repo / "a.txt").write_text("theirs\n"); sh(repo, "commit", "-qam", "theirs")
    sh(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("mine\n"); sh(repo, "commit", "-qam", "mine")
    import subprocess
    subprocess.run(["git", "merge", "other"], cwd=repo, capture_output=True)
    s, g = look(repo)
    sg = suggest.rules(s, g)
    assert sg.menu == "resolve" and "1 file" in sg.title and suggest.allowed(s, sg) == []
    (repo / "a.txt").write_text("both\n"); sh(repo, "add", ".")
    sg = suggest.rules(*look(repo))
    assert sg.menu == "finish_merge"


def test_signature_follows_edits(repo):
    before = suggest.signature(git.state(repo))
    assert before == suggest.signature(git.state(repo))
    (repo / "a.txt").write_text("edited, and longer\n")
    edited = suggest.signature(git.state(repo))
    (repo / "a.txt").write_text("edited again, longer still\n")
    assert len({before, edited, suggest.signature(git.state(repo))}) == 3


# ---- what the AI may change

def pick(**kw):
    return ai.NextStep(**{"action": "save", "reason": "Because.", "message": "", "groups": [], "branch": "", **kw})


def dirty(repo):
    (repo / "a.txt").write_text("edited\n")
    (repo / "pay.py").write_text("x")
    (repo / "docs.md").write_text("x")
    s, g = look(repo)
    base = suggest.rules(s, g)
    return s, base, suggest.allowed(s, base)


def test_ai_writes_the_description(repo):
    s, base, actions_ = dirty(repo)
    sg = suggest._checked(pick(message='"Add payments"\nextra line'), s, base, actions_)
    assert (sg.kind, sg.source, sg.message) == ("save", "ai", "Add payments") and "Add payments" in sg.title


def test_ai_split_must_cover_every_file_once(repo):
    s, base, actions_ = dirty(repo)
    assert "split_save" in actions_
    good = [ai.NextGroup(message="Fix a", files=["a.txt"]), ai.NextGroup(message="Add payments", files=["pay.py", "docs.md"])]
    sg = suggest._checked(pick(action="split_save", groups=good), s, base, actions_)
    assert sg.kind == "split_save" and [g.files for g in sg.groups] == [["a.txt"], ["pay.py", "docs.md"]]
    assert suggest.ghost(sg, s).saves == 2
    for bad in (
        [good[0]],  # only one group
        [good[0], ai.NextGroup(message="Add payments", files=["pay.py"])],  # docs.md forgotten
        [good[0], ai.NextGroup(message="Add payments", files=["pay.py", "docs.md", "a.txt"])],  # a.txt twice
        [good[0], ai.NextGroup(message="Add payments", files=["pay.py", "docs.md", "made-up.py"])],
        [good[0], ai.NextGroup(message="", files=["pay.py", "docs.md"])],
    ):
        assert suggest._checked(pick(action="split_save", groups=bad), s, base, actions_) is base


def test_ai_cannot_step_outside_the_list(repo):
    s, base, actions_ = dirty(repo)
    for action in ("combine", "nothing", "force_push", ""):
        assert suggest._checked(pick(action=action, message="x"), s, base, actions_) is base
    assert suggest._checked(pick(action="save", message=""), s, base, actions_) is base


def test_ai_branch_name_is_tidied_and_must_be_new(repo):
    s, base, actions_ = dirty(repo)
    sg = suggest._checked(pick(action="new_branch", branch="Payments Page!"), s, base, actions_)
    assert (sg.kind, sg.branch) == ("new_branch", "payments-page")
    assert suggest.ghost(sg, s) == graph.Ghost(branch="payments-page", saves=1)
    assert suggest._checked(pick(action="new_branch", branch="main"), s, base, actions_) is base


def test_ai_branch_off_a_feature_branch_starts_from_main(repo):
    sh(repo, "switch", "-qc", "dark-mode")
    commit(repo, "theme")
    (repo / "hikes.js").write_text("x")  # nothing to do with dark mode
    s, g = look(repo)
    base = suggest.rules(s, g)
    sg = suggest._checked(pick(action="new_branch", branch="add-hikes"), s, base, suggest.allowed(s, base), g)
    assert (sg.kind, sg.branch, sg.target) == ("new_branch", "add-hikes", "main") and "(from main)" in sg.title
    assert suggest.ghost(sg, s) == graph.Ghost(branch="add-hikes", saves=1, fork="main")
    # Edits to a file this branch changed can't move to main without it: start here instead.
    (repo / "theme.txt").write_text("darker")
    s, g = look(repo)
    sg = suggest._checked(pick(action="new_branch", branch="add-hikes"), s, base, suggest.allowed(s, base), g)
    assert sg.target == "" and "from" not in sg.title


def test_ai_branch_on_main_starts_here(repo):
    s, base, actions_ = dirty(repo)
    sg = suggest._checked(pick(action="new_branch", branch="payments"), s, base, actions_, graph.build(s))
    assert sg.target == ""


def test_ai_can_say_a_branch_is_not_done(repo):
    sh(repo, "switch", "-qc", "login")
    commit(repo, "wip login")
    s, g = look(repo)
    base = suggest.rules(s, g)  # no remote here, so the branch goes straight to "combine"
    assert base.kind == "merge"
    sg = suggest._checked(pick(action="nothing", reason="The last save is labelled wip."), s, base, ["combine", "nothing"])
    assert not sg.acceptable and sg.source == "ai" and "wip" in sg.reason
    sg = suggest._checked(pick(action="combine"), s, base, ["combine", "nothing"])
    assert (sg.kind, sg.source, sg.branch, sg.target) == ("merge", "ai", "login", "main")


def test_from_ai_sends_branch_work_and_the_allowed_list(repo, monkeypatch):
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    s, g = look(repo)
    seen = {}

    def fake(s, facts, with_changes):
        seen.update(facts=facts, with_changes=with_changes)
        return pick(action="combine", reason="The login form is complete."), {"model": "m"}

    monkeypatch.setattr(ai, "suggest_next", fake)
    sg, tel = suggest.from_ai(s, g, suggest.rules(s, g))
    assert "login form" in seen["facts"] and "combine, nothing" in seen["facts"] and not seen["with_changes"]
    assert sg.reason == "The login form is complete." and tel["used"] and tel["step"] == "merge"


def test_split_save_plan_saves_everything(repo):
    (repo / "a.txt").write_text("edited\n")
    (repo / "pay.py").write_text("x")
    sh(repo, "add", "pay.py")  # half-marked: the plan unmarks first so each save gets only its files
    s = git.state(repo)
    plan = actions.split_save(s, [("Fix a", ["a.txt"]), ("Add payments", ["pay.py"])])
    assert plan.runnable and plan.risk == "safe"
    for st in plan.steps:
        assert git.run(st.args, repo).ok
    assert sh(repo, "log", "--format=%s").splitlines() == ["Add payments", "Fix a", "first"]
    assert sh(repo, "show", "--name-only", "--format=", "HEAD~1").split() == ["a.txt"]
    assert not git.state(repo).dirty
