"""Pre-save check: findings, the leave-out plan, and secrets kept from the AI."""

import os

from gitly import git, precheck, secretscan
from gitly.plan import Step

from conftest import sh

FAKE_STRIPE = "sk_live_" + "a1B2c3D4e5F6g7H8i9J0"  # built at runtime so this file has no "key"


def run_plan(repo, plan):
    for st in plan.steps:
        r = git.append_gitignore(repo, st.gitignore) if st.gitignore else git.run(st.args, repo)
        assert r.ok, (st.display, r.output)


def kinds(findings):
    return sorted((f.kind, f.path) for f in findings)


def test_clean_save_has_no_findings(repo):
    (repo / "a.txt").write_text("edited\n")
    (repo / "new.py").write_text("print('hi')\n")
    assert precheck.check(git.state(repo)) == []


def test_finds_secret_file_junk_and_key(repo):
    (repo / ".env").write_text("STRIPE=x\n")
    (repo / ".env.example").write_text("STRIPE=\n")  # template: fine to save
    (repo / "web" / "node_modules" / "lib").mkdir(parents=True)
    (repo / "web" / "node_modules" / "lib" / "i.js").write_text("x")
    (repo / "web" / "node_modules" / "j.js").write_text("x")
    (repo / ".DS_Store").write_bytes(b"\0\1")
    (repo / "pay.py").write_text(f"import stripe\nstripe.api_key = '{FAKE_STRIPE}'\n")
    found = precheck.check(git.state(repo))
    assert kinds(found) == [
        ("junk", ".DS_Store"), ("junk", "web/node_modules/"),
        ("secret_file", ".env"), ("secret_text", "pay.py"),
    ]
    by_path = {f.path: f for f in found}
    assert by_path["web/node_modules/"].gitignore == "node_modules/"
    assert "Line 2" in by_path["pay.py"].detail and "Stripe" in by_path["pay.py"].detail
    assert FAKE_STRIPE not in by_path["pay.py"].detail  # never echo the key
    assert by_path["pay.py"].gitignore is None


def test_key_added_to_tracked_file_reports_line(repo):
    (repo / "a.txt").write_text("one\ntwo\nkey = " + "AKIA" + "ABCDEFGHIJKLMNOP\n")
    [f] = precheck.check(git.state(repo))
    assert f.kind == "secret_text" and f.path == "a.txt" and "Line 3" in f.detail and "AWS" in f.detail


def test_tracked_secret_file_is_not_gitignored(repo):
    (repo / ".env").write_text("A=1\n")
    sh(repo, "add", "."); sh(repo, "commit", "-qm", "oops")
    (repo / ".env").write_text("A=2\n")
    [f] = precheck.check(git.state(repo))
    assert f.kind == "secret_file" and f.gitignore is None and "history" in f.detail


def test_large_file(repo, monkeypatch):
    monkeypatch.setattr(precheck, "LARGE_BYTES", 1000)
    (repo / "video.mp4").write_bytes(b"\0" * 2000)
    [f] = precheck.check(git.state(repo))
    assert f.kind == "large" and f.gitignore == "video.mp4"


def test_leave_out_plan_saves_only_the_rest(repo):
    (repo / "a.txt").write_text("edited\n")
    (repo / ".env").write_text("A=1\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x")
    (repo / "pay.py").write_text(f"KEY = '{FAKE_STRIPE}'\n")
    sh(repo, "add", ".env")  # already staged: must be taken back out
    s = git.state(repo)
    found = precheck.check(s)
    plan = precheck.save_leaving_out(s, found, "Edit a.txt")
    assert plan.runnable and plan.risk == "safe"
    assert plan.steps[0].gitignore == [".env", "node_modules/"]
    run_plan(repo, plan)
    committed = sh(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == [".gitignore", "a.txt"]
    assert (repo / ".env").exists() and (repo / "pay.py").exists()  # nothing deleted
    gi = (repo / ".gitignore").read_text()
    assert ".env\n" in gi and "node_modules/\n" in gi
    # The warning doesn't come back for ignored files; the key in pay.py still does.
    assert kinds(precheck.check(git.state(repo))) == [("secret_text", "pay.py")]


def test_leave_out_before_first_commit(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("print(1)\n")
    (tmp_path / ".env").write_text("A=1\n")
    sh(tmp_path, "add", ".")
    s = git.state(tmp_path)
    plan = precheck.save_leaving_out(s, precheck.check(s), "First save")
    assert ["rm", "--cached", "-r", "-q", "--", ".env"] in [st.args for st in plan.steps]
    run_plan(tmp_path, plan)
    assert sorted(sh(tmp_path, "show", "--name-only", "--format=", "HEAD").split()) == [".gitignore", "app.py"]


def test_special_characters_use_pathspec_not_gitignore(repo):
    (repo / "keys").mkdir()
    (repo / "keys" / "a*b.pem").write_text("x")
    s = git.state(repo)
    found = precheck.check(s)
    [pem] = [f for f in found if f.path == "keys/a*b.pem"]
    assert pem.gitignore is None  # "*" would ignore more than this one file
    plan = precheck.save_leaving_out(s, found, "x")
    assert any(":(exclude,literal)keys/a*b.pem" in st.args for st in plan.steps)


def test_without_hides_left_out_files_from_ai(repo):
    (repo / "a.txt").write_text("edited\n")
    (repo / ".env").write_text("A=1\n")
    s = git.state(repo)
    trimmed = precheck.without(s, precheck.check(s))
    assert trimmed.untracked == [] and trimmed.left_out == [".env"]
    assert ".env" not in git.working_diff(trimmed)
    assert "left out of this save: .env" in trimmed.as_context()


def test_working_diff_redacts_keys(repo):
    (repo / "pay.py").write_text(f"KEY = '{FAKE_STRIPE}'\n")
    d = git.working_diff(git.state(repo))
    assert FAKE_STRIPE not in d and "[secret removed by gitly]" in d


def test_secret_patterns():
    assert secretscan.is_secret_file(".env") and secretscan.is_secret_file("api/.env.local")
    assert not secretscan.is_secret_file(".env.example")
    assert not secretscan.is_secret_file("environment.py")
    assert secretscan.find("token = 'gh" + "p_" + "A" * 36 + "'") == "a GitHub token"
    assert secretscan.find("-----BEGIN OPENSSH PRIVATE KEY-----") == "a private key"
    assert secretscan.find("sk-ant-" + "x" * 30) == "an Anthropic API key"
    assert secretscan.find("skip_live_tests = True") is None


def test_gitignore_step(repo):
    (repo / ".gitignore").write_text("dist/")  # no trailing newline
    r = git.append_gitignore(repo, ["dist/", ".env"])
    assert r.ok
    assert (repo / ".gitignore").read_text() == "dist/\n\n# Added by gitly: keep these out of your saves\n.env\n"
    assert Step([], "x", gitignore=["ok", "bad\nline"]).blocked
    assert Step([], "x", gitignore=[".env"]).display == "add to .gitignore: .env"


def _conflict(repo):
    sh(repo, "switch", "-qc", "login")
    (repo / "a.txt").write_text("login version\n"); sh(repo, "commit", "-qam", "login edit")
    sh(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("main version\n"); sh(repo, "commit", "-qam", "main edit")
    assert not git.run(["merge", "login"], repo).ok


def test_leftover_markers(repo):
    _conflict(repo)
    (repo / "notes.md").write_text("Install\n=======\n")  # a heading, not a conflict
    assert precheck.leftover_markers(git.state(repo)) == [("a.txt", 1)]
    # Still caught after the file is marked as resolved without being fixed.
    sh(repo, "add", "--all")
    assert precheck.leftover_markers(git.state(repo)) == [("a.txt", 1)]
    (repo / "a.txt").write_text("both versions\n")
    assert precheck.leftover_markers(git.state(repo)) == []
