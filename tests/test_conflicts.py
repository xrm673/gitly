"""Reading conflict spots, naming the sides, and turning picks into plans that work."""

import subprocess

from gitly import conflicts, git
from gitly.conflicts import COMBINED, INCOMING, INCOMING_FIRST, MINE, MINE_FIRST, SKIP, Advice

from conftest import sh

MID = "m1\nm2\nm3\nm4\n"  # far enough apart that git makes two spots
TWO_SPOTS = f"top\nx = 1\n{MID}y = 1\nend\n"


def _clash(repo, base=TWO_SPOTS, ours=f"top\nx = 3\n{MID}y = 3\nend\n",
           theirs=f"top\nx = 2\n{MID}y = 2\nend\n", path="f.txt"):
    """main and login both change `path`; returns with main checked out."""
    (repo / path).write_text(base); sh(repo, "add", "."); sh(repo, "commit", "-qm", "base")
    sh(repo, "switch", "-qc", "login")
    (repo / path).write_text(theirs); sh(repo, "commit", "-qam", "login change")
    sh(repo, "switch", "-q", "main")
    (repo / path).write_text(ours); sh(repo, "commit", "-qam", "main change")


def _merge(repo, *args):
    assert not git.run(["merge", *args, "login"], repo).ok


def _run(repo, plan):
    for st in plan.steps:
        r = conflicts.write(str(repo), st) if st.write is not None else git.run(st.args, repo)
        assert r.ok, (st.display, r.output)


def test_parse_spots_and_context():
    text = "a\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> login\nb\n"
    pieces = conflicts.parse("f", text)
    sp = pieces[1]
    assert pieces[0] == "a\n" and pieces[2] == "b\n"
    assert (sp.mine, sp.incoming, sp.line, sp.base) == ("mine\n", "theirs\n", 2, None)
    assert sp.before == "a\n" and sp.after == "b\n" and sp.raw == text[2:-2]
    flipped = conflicts.parse("f", text, flipped=True)[1]
    assert (flipped.mine, flipped.incoming) == ("theirs\n", "mine\n")


def test_parse_diff3_and_crlf():
    text = "<<<<<<< HEAD\r\nm\r\n||||||| base\r\no\r\n=======\r\nt\r\n>>>>>>> x\r\n"
    sp = conflicts.parse("f", text)[0]
    assert (sp.mine, sp.base, sp.incoming) == ("m\r\n", "o\r\n", "t\r\n")


def test_parse_rejects_broken_markers():
    assert conflicts.parse("f", "<<<<<<< HEAD\nm\n=======\nt\n") is None  # never closed
    assert conflicts.parse("f", "<<<<<<< a\n<<<<<<< b\n") is None
    assert conflicts.parse("f", "stray\n>>>>>>> x\n") is None
    assert conflicts.parse("f", "Title\n=======\n") == ["Title\n=======\n"]  # a heading, not a conflict


def test_merge_sides_and_history(repo):
    _clash(repo)
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    assert (sd.mine, sd.incoming, sd.op, sd.flipped) == ("main", "login", "merge", False)
    f = files[0]
    assert [(sp.mine, sp.incoming) for sp in f.spots] == [("x = 3\n", "x = 2\n"), ("y = 3\n", "y = 2\n")]
    assert conflicts.history(str(repo), sd, "f.txt") == (["main change"], ["login change"])


def test_rebase_swaps_which_side_is_yours(repo):
    _clash(repo)
    sh(repo, "switch", "-q", "login")
    assert not git.run(["rebase", "main"], repo).ok
    files, sd = conflicts.load(git.state(repo))
    assert sd.op == "rebase" and sd.mine == "your save “login change”" and sd.incoming == "main"
    assert files[0].spots[0].mine == "x = 2\n"  # login's own edit, though git wrote it at the bottom
    plan = conflicts.plan(files, sd, {("f.txt", 1): MINE, ("f.txt", 2): MINE}, op_name="rebase")
    _run(repo, plan)
    assert (repo / "f.txt").read_text() == f"top\nx = 2\n{MID}y = 2\nend\n"
    assert git.state(repo).conflicts == []


def test_pull_names_github(remote_pair):
    repo, other = remote_pair
    (other / "a.txt").write_text("theirs\n"); sh(other, "commit", "-qam", "t"); sh(other, "push", "-q")
    (repo / "a.txt").write_text("mine\n"); sh(repo, "commit", "-qam", "m")
    assert not git.run(["pull", "--no-rebase"], repo).ok
    _, sd = conflicts.load(git.state(repo))
    assert (sd.op, sd.incoming) == ("pull", "GitHub's version")


def test_every_pick_writes_the_right_text(repo):
    _clash(repo)
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    sp1, sp2 = files[0].spots
    sp2.advice = Advice(pick=COMBINED, combined="y = 2  # and 3")  # no newline: gitly adds one
    plan = conflicts.plan(files, sd, {("f.txt", 1): INCOMING_FIRST, ("f.txt", 2): COMBINED})
    assert [st.display for st in plan.steps] == ["write f.txt", "git add -- f.txt"]
    assert "both, incoming first" in plan.note and "combined" in plan.note
    _run(repo, plan)
    assert (repo / "f.txt").read_text() == f"top\nx = 2\nx = 3\n{MID}y = 2  # and 3\nend\n"
    s = git.state(repo)
    assert s.conflicts == [] and s.merging


def test_skipped_spot_keeps_markers_and_stays_conflicted(repo):
    _clash(repo)
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    plan = conflicts.plan(files, sd, {("f.txt", 1): MINE_FIRST})  # spot 2 missing: skipped
    assert [st.display for st in plan.steps] == ["write f.txt"]  # not marked as sorted out
    _run(repo, plan)
    text = (repo / "f.txt").read_text()
    assert text.startswith(f"top\nx = 3\nx = 2\n{MID}<<<<<<< HEAD\ny = 3\n")
    files, _ = conflicts.load(git.state(repo))
    assert [sp.n for sp in files[0].spots] == [1]  # the one left


def test_everything_skipped_means_no_steps(repo):
    _clash(repo)
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    assert conflicts.plan(files, sd, {("f.txt", 1): SKIP}).steps == []


def test_fixed_by_hand_only_needs_marking(repo):
    _clash(repo)
    _merge(repo)
    (repo / "f.txt").write_text("sorted\n")
    files, sd = conflicts.load(git.state(repo))
    assert files[0].done_by_hand
    plan = conflicts.plan(files, sd, {})
    assert [st.display for st in plan.steps] == ["git add -- f.txt"]


def test_deleted_on_one_side(repo):
    (repo / "d.txt").write_text("d\n"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "d")
    sh(repo, "switch", "-qc", "login")
    (repo / "d.txt").write_text("edited\n"); sh(repo, "commit", "-qam", "edit d")
    sh(repo, "switch", "-q", "main")
    sh(repo, "rm", "-q", "d.txt"); sh(repo, "commit", "-qm", "drop d")
    _merge(repo)
    s = git.state(repo)
    assert s.conflict_kinds == {"d.txt": "DU"}
    files, sd = conflicts.load(s)
    sp = files[0].spots[0]
    assert sp.whole_file and sp.mine is None and sp.incoming == "edited\n"
    keep = conflicts.plan(files, sd, {("d.txt", 1): INCOMING})
    assert [st.display for st in keep.steps] == ["write d.txt", "git add -- d.txt"]
    drop = conflicts.plan(files, sd, {("d.txt", 1): MINE})
    assert [st.display for st in drop.steps] == ["git rm --quiet -- d.txt"]
    _run(repo, drop)
    assert not (repo / "d.txt").exists() and git.state(repo).conflicts == []


def test_binary_keeps_one_whole_side(repo):
    (repo / "img.bin").write_bytes(b"\0base"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "img")
    sh(repo, "switch", "-qc", "login")
    (repo / "img.bin").write_bytes(b"\0login"); sh(repo, "commit", "-qam", "login img")
    sh(repo, "switch", "-q", "main")
    (repo / "img.bin").write_bytes(b"\0main"); sh(repo, "commit", "-qam", "main img")
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    sp = files[0].spots[0]
    assert sp.whole_file and sp.binary and sp.options() == [MINE, INCOMING, SKIP]
    _run(repo, conflicts.plan(files, sd, {("img.bin", 1): INCOMING}))
    assert (repo / "img.bin").read_bytes() == b"\0login"


def test_unreadable_markers_fall_back_to_whole_file(repo):
    _clash(repo)
    _merge(repo)
    (repo / "f.txt").write_text("<<<<<<< HEAD\nhalf edited\n")
    files, _ = conflicts.load(git.state(repo))
    sp = files[0].spots[0]
    assert sp.whole_file and sp.unreadable and sp.incoming == f"top\nx = 2\n{MID}y = 2\nend\n"


def test_recommended_pick_comes_first():
    sp = conflicts.Spot("f", 1, "a\n", "b\n")
    assert sp.options() == [MINE, INCOMING, MINE_FIRST, INCOMING_FIRST, SKIP]
    sp.advice = Advice(pick=COMBINED, combined="ab\n")
    assert sp.options()[0] == COMBINED and sp.options()[-1] == SKIP
    sp.advice = Advice(pick=INCOMING)
    assert sp.options()[:2] == [INCOMING, MINE]


def test_changed_since_read(repo):
    _clash(repo)
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    plan = conflicts.plan(files, sd, {("f.txt", 1): MINE, ("f.txt", 2): MINE})
    assert conflicts.changed_since_read(str(repo), plan) == []
    (repo / "f.txt").write_text("someone else's edit\n")
    assert conflicts.changed_since_read(str(repo), plan) == ["f.txt"]


def test_write_step_stays_in_the_project(repo):
    from gitly.plan import Step
    assert Step([], "x", write=b"", path="../evil").blocked
    assert Step([], "x", write=b"", path="/etc/passwd").blocked
    assert Step([], "x", write=b"", path="src/a.py").blocked is None


def test_clean_combined():
    assert conflicts.clean_combined("```python\nx = 1\n```\n") == "x = 1\n"
    assert conflicts.clean_combined("<<<<<<< HEAD\nx\n") is None
    assert conflicts.clean_combined("a\n=======\nb\n") is None
    assert conflicts.clean_combined("  \n") is None


def test_revert_sides(repo):
    (repo / "a.txt").write_text("two\n"); sh(repo, "commit", "-qam", "two")
    (repo / "a.txt").write_text("three\n"); sh(repo, "commit", "-qam", "three")
    subprocess.run(["git", "revert", "--no-edit", "HEAD~1"], cwd=repo, capture_output=True)
    files, sd = conflicts.load(git.state(repo))
    assert sd.op == "revert" and sd.incoming == "undoing “two”"
    assert conflicts.history(str(repo), sd, "a.txt")[1] == ["Undo of: two"]


def test_both_keeps_the_blank_lines_between_blocks(repo):
    base = "def add(a, b):\n    return a + b\n"
    _clash(repo, base=base, ours=base + "\n\ndef sub(a, b):\n    return a - b\n",
           theirs=base + "\n\ndef mul(a, b):\n    return a * b\n", path="u.py")
    _merge(repo)
    files, sd = conflicts.load(git.state(repo))
    _run(repo, conflicts.plan(files, sd, {("u.py", 1): MINE_FIRST}))
    assert (repo / "u.py").read_text() == (base + "\n\ndef sub(a, b):\n    return a - b\n"
                                           "\n\ndef mul(a, b):\n    return a * b\n")


def test_both_adds_no_gap_inside_a_list():
    sp = conflicts.Spot("f", 1, "import b\n", "import c\n", before="import a\n")
    assert sp.text_for(MINE_FIRST) == "import b\nimport c\n"
