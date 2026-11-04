from gitly import git, rescue

from conftest import sh


def fail(repo, args):
    r = git.run(args, repo)
    assert not r.ok, r.output
    return rescue.diagnose(r, git.state(repo))


def test_push_rejected(remote_pair):
    repo, other = remote_pair
    (other / "o.txt").write_text("o"); sh(other, "add", "."); sh(other, "commit", "-qm", "o"); sh(other, "push", "-q")
    (repo / "r.txt").write_text("r"); sh(repo, "add", "."); sh(repo, "commit", "-qm", "r")
    d = fail(repo, ["push"])
    assert d.title.startswith("GitHub has changes")
    # the offered fix actually works
    for st in d.fixes[0].steps:
        assert git.run(st.args, repo).ok, st.args


def test_conflict(remote_pair):
    repo, other = remote_pair
    (other / "a.txt").write_text("theirs\n"); sh(other, "commit", "-qam", "t"); sh(other, "push", "-q")
    (repo / "a.txt").write_text("mine\n"); sh(repo, "commit", "-qam", "m")
    d = fail(repo, ["pull", "--no-rebase"])
    assert "overlap" in d.title
    assert git.state(repo).conflicts == ["a.txt"]
    abort = d.fixes[1]
    for st in abort.steps:
        assert git.run(st.args, repo).ok
    assert not git.state(repo).merging


def test_no_upstream(remote_pair):
    repo, _ = remote_pair
    sh(repo, "switch", "-qc", "feature")
    d = fail(repo, ["push"])
    assert "never been uploaded" in d.title
    assert d.fixes[0].steps[0].args == ["push", "--set-upstream", "origin", "feature"]
    assert git.run(d.fixes[0].steps[0].args, repo).ok


def test_nothing_to_commit(repo):
    d = fail(repo, ["commit", "-m", "x"])
    assert d.title == "Nothing to save"


def test_would_overwrite(repo):
    sh(repo, "switch", "-qc", "other")
    (repo / "a.txt").write_text("other\n"); sh(repo, "commit", "-qam", "o")
    sh(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("dirty\n")
    d = fail(repo, ["switch", "other"])
    assert "in the way" in d.title


def test_no_remote(repo):
    d = fail(repo, ["push"])
    assert d.needs_input == "remote_url"


def test_identity(tmp_path, monkeypatch):
    for k in ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("EMAIL", "")
    sh(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("x"); sh(tmp_path, "add", ".")
    monkeypatch.setattr("socket.getfqdn", lambda: "localhost")
    r = git.run(["commit", "-m", "x"], tmp_path)
    if r.ok:  # some systems auto-detect an identity; nothing to rescue then
        return
    d = rescue.diagnose(r, git.state(tmp_path))
    assert d.needs_input == "identity"
