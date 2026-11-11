"""Branch map: lanes, ownership of shared commits, and the rendered picture."""

import itertools
import os

from rich.text import Text

from gitly import git, graph

from conftest import sh


_clock = itertools.count(2_000_000_000, 60)


def commit(repo, name):
    """Commit with a distinct, increasing timestamp (real repos never tie)."""
    (repo / f"{name}.txt").write_text(name)
    sh(repo, "add", "-A")
    when = f"@{next(_clock)} +0000"
    os.environ["GIT_COMMITTER_DATE"] = os.environ["GIT_AUTHOR_DATE"] = when
    try:
        sh(repo, "commit", "-qm", name)
    finally:
        del os.environ["GIT_COMMITTER_DATE"], os.environ["GIT_AUTHOR_DATE"]


def picture(repo):
    s = git.state(repo)
    g = graph.build(s)
    return g, Text.from_markup(graph.render(g, s)).plain.splitlines()


def lane(g, name):
    return next(l for l in g.lanes if l.name == name)


def test_single_branch(repo):
    commit(repo, "second")
    g, lines = picture(repo)
    assert [l.name for l in g.lanes] == ["main"]
    assert [c.subject for c in lane(g, "main").commits] == ["first", "second"]
    [line] = lines
    assert line.startswith("▶ main") and "●──◉" in line and "you are here" in line
    assert "GitHub" not in line  # no remote: don't talk about GitHub


def test_no_commits_yet(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    _, lines = picture(tmp_path)
    assert "No saves yet" in lines[0]


def test_branch_grows_from_trunk(repo):
    commit(repo, "second")
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    sh(repo, "switch", "-q", "main")
    commit(repo, "fix footer")
    g, lines = picture(repo)
    login = lane(g, "login")
    assert [c.subject for c in login.commits] == ["login form"]
    assert g.commit(login.fork).subject == "second"
    main_line, login_line = lines
    assert main_line.startswith("▶ main") and "you are here" in main_line
    # login's row starts under the commit it grew out of, and its commit sits left
    # of "fix footer" because it's older.
    assert login_line.index("╰") == main_line.index("●", main_line.index("●") + 1)
    assert login_line.index("●") < main_line.index("◉")


def test_shared_commits_belong_to_the_older_branch(repo):
    sh(repo, "switch", "-qc", "payments")
    commit(repo, "cart")
    sh(repo, "switch", "-qc", "coupons")  # current branch, nothing of its own yet
    g, lines = picture(repo)
    assert [c.subject for c in lane(g, "payments").commits] == ["cart"]
    coupons = lane(g, "coupons")
    assert coupons.commits == [] and g.commit(coupons.fork).subject == "cart"
    assert [l.name for l in g.lanes] == ["main", "coupons", "payments"]  # current branch first
    assert "no saves of its own yet" in lines[1] and "you are here" in lines[1]


def test_merged_branch(repo):
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    sh(repo, "switch", "-q", "main")
    commit(repo, "other work")
    sh(repo, "merge", "-q", "--no-edit", "login")
    g, lines = picture(repo)
    assert lane(g, "main").commits[-1].is_merge
    assert lane(g, "login").merged
    assert "◆" in lines[0] or "◉" in lines[0]
    assert "already combined into main" in lines[1]


def test_unsaved_changes_hang_off_you_are_here(repo):
    (repo / "a.txt").write_text("edited\n")
    (repo / "new.txt").write_text("x")
    _, lines = picture(repo)
    assert "✎ 2 unsaved files" in lines[0]


def test_long_history_is_cut_with_marker(repo):
    for i in range(12):
        commit(repo, f"c{i}")
    g, lines = picture(repo)
    assert len(lane(g, "main").commits) == graph.TRUNK_COMMITS and lane(g, "main").truncated
    assert "┄" in lines[0]


def test_detached_head(repo):
    commit(repo, "second")
    sh(repo, "switch", "-q", "--detach", "HEAD~1")
    commit(repo, "experiment")
    g, lines = picture(repo)
    d = lane(g, "(no branch)")
    assert [c.subject for c in d.commits] == ["experiment"] and d.current
    assert "old save, not on a branch" in lines[-1] and "you are here" in lines[-1]


def test_github_status(remote_pair):
    repo, other = remote_pair
    commit(other, "teammate")
    sh(other, "push", "-q")
    sh(repo, "fetch", "-q")
    g, lines = picture(repo)
    main = lane(g, "main")
    assert main.commits[-1].remote_only and main.commits[-1].subject == "teammate"
    assert "○" in lines[0] and "↓1 on GitHub only" in lines[0]

    commit(repo, "mine")  # now both sides have new commits
    g, lines = picture(repo)
    assert [l.name for l in g.lanes] == ["main", "main on GitHub"]
    assert "↑1 not uploaded" in lines[0] and "○" in lines[1]


def test_not_uploaded_branch(remote_pair):
    repo, _ = remote_pair
    sh(repo, "switch", "-qc", "draft")
    commit(repo, "wip")
    _, lines = picture(repo)
    assert "✓ on GitHub" in lines[0] and "not on GitHub yet" in lines[1]


def test_parents_left_of_children():
    a = graph.Commit("a", [], 300, "a")  # clock skew: parent "newer" than child
    b = graph.Commit("b", ["a"], 100, "b")
    g = graph.Graph("main", [graph.Lane("main", "trunk", [a, b])], "b")
    col = graph.columns(g)
    assert col["a"] < col["b"]


def test_positions_and_previews(repo):
    commit(repo, "second")
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    sh(repo, "switch", "-q", "main")
    s = git.state(repo)
    g = graph.build(s)
    cells = graph.positions(g)
    assert [len(c) for c in cells] == [2, 1]  # main: first, second; login: login form

    first = lane(g, "main").commits[0].sha
    new = graph.after_new_branch(g, "idea", first)
    idea = lane(new, "idea")
    assert idea.current and idea.fork == first and new.head == first
    assert not lane(new, "main").current and lane(g, "main").current  # original untouched
    text = Text.from_markup(graph.render(new, s, compact=True)).plain
    assert "idea" in text and "new branch" in text and "in sync" not in text

    moved = graph.after_switch(g, "login")
    assert lane(moved, "login").current and moved.head == lane(g, "login").tip

    saved = graph.after_save(g, "wip")
    assert lane(saved, "main").commits[-1].subject == "wip" and saved.head == "(new save)"


def test_render_highlights_selection(repo):
    commit(repo, "second")
    s = git.state(repo)
    g = graph.build(s)
    x = graph.positions(g)[0][0][0]
    assert graph.SELECTED in graph.render(g, s, selected=(0, x))
    assert graph.SELECTED not in graph.render(g, s)


def test_ago():
    assert graph.ago(1000, now=1000) == "just now"
    assert graph.ago(1000, now=1000 + 3 * 3600) == "3 hours ago"
    assert graph.ago(1000, now=1000 + 86400) == "1 day ago"


def test_merge_and_undo_previews(repo):
    commit(repo, "second")
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    sh(repo, "switch", "-q", "main")
    s = git.state(repo)
    g = graph.build(s)

    merged = graph.after_merge(g, "login", "main")
    main = lane(merged, "main")
    assert main.commits[-1].is_merge and main.commits[-1].parents == [lane(g, "main").tip, lane(g, "login").tip]
    assert merged.head == "(merge)" and "combined into main" in lane(merged, "login").badge

    from_login = graph.after_merge(graph.after_switch(g, "login"), "login", "main")
    assert lane(from_login, "main").current  # you end up on the receiving branch

    reverted = graph.after_revert(g, "second")
    assert lane(reverted, "main").commits[-1].subject == "Undo “second”"

    un = graph.after_uncommit(g)
    assert [c.subject for c in lane(un, "main").commits] == ["first"]
    assert un.head == lane(g, "main").commits[0].sha
    assert lane(g, "main").commits[-1].subject == "second"  # original untouched


def test_rows_say_what_their_latest_save_was(repo):
    commit(repo, "add the pricing page")
    _, [line] = picture(repo)
    assert "“add the pricing page”" in line


def test_notes_are_cut_to_the_width(repo):
    commit(repo, "a very long description of what this save was about, really")
    s = git.state(repo)
    g = graph.build(s)
    for width in (40, 60, 90):
        [line] = Text.from_markup(graph.render(g, s, max_width=width)).plain.splitlines()
        assert len(line) <= width and "you are here" in line
    assert "…" in Text.from_markup(graph.render(g, s, max_width=70)).plain


def test_ghost_saves_follow_you_are_here(repo):
    commit(repo, "second")
    s = git.state(repo)
    g = graph.build(s)
    [line] = Text.from_markup(graph.render(g, s, ghost=graph.Ghost(saves=2))).plain.splitlines()
    assert "●──◉╌╌◌╌╌◌" in line
    assert "◌" not in graph.render(g, s)


def test_ghost_merge_joins_the_two_rows(repo):
    commit(repo, "second")
    sh(repo, "switch", "-qc", "login")
    commit(repo, "login form")
    s = git.state(repo)
    g = graph.build(s)
    main, login = Text.from_markup(graph.render(g, s, ghost=graph.Ghost(merge=("login", "main")))).plain.splitlines()
    assert "●╌╌╌╌╌◌" in main and "◉╌╌╯" in login
    assert main.index("◌") == login.index("╯")  # the combined save sits where the two lines meet
    # A ghost never changes what the cursor can land on.
    assert graph.positions(g) == graph.positions(graph.build(git.state(repo)))


def test_ghost_branch_gets_its_own_row(repo):
    commit(repo, "second")
    (repo / "x.txt").write_text("x")
    s = git.state(repo)
    g = graph.build(s)
    lines = Text.from_markup(graph.render(g, s, ghost=graph.Ghost(branch="payments", saves=1))).plain.splitlines()
    assert len(lines) == 2 and lines[1].startswith("  payments") and "╰╌╌◌" in lines[1]
    assert lines[1].index("╰") == lines[0].index("◉")  # it grows out of where you are


def test_two_previewed_saves_are_distinct(repo):
    s = git.state(repo)
    twice = graph.after_save(graph.after_save(graph.build(s), "one"), "two")
    assert [c.subject for c in lane(twice, "main").commits] == ["first", "one", "two"]
    assert len(graph.columns(twice)) == 3
