from gitly import actions, git

from conftest import sh


def test_clean_state(repo):
    s = git.state(repo)
    assert s.branch == "main" and s.head_sha and not s.dirty
    assert "Everything is saved" in s.summary()


def test_not_a_repo(tmp_path):
    assert git.state(tmp_path) is None


def test_changes_parsed(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "new file.txt").write_text("x")
    sh(repo, "mv", "a.txt", "b.txt")
    s = git.state(repo)
    assert "new file.txt" in s.untracked
    assert any(c.path == "b.txt" and c.status == "renamed" and c.staged for c in s.changes)
    assert any(c.path == "b.txt" and not c.staged for c in s.changes)


def test_save_and_ahead_behind(remote_pair):
    repo, other = remote_pair
    (other / "o.txt").write_text("o")
    sh(other, "add", "."); sh(other, "commit", "-qm", "other"); sh(other, "push", "-q")
    sh(repo, "fetch", "-q")
    (repo / "a.txt").write_text("changed\n")
    for st in actions.save_work(git.state(repo), "my change").steps:
        assert git.run(st.args, repo).ok
    s = git.state(repo)
    assert (s.ahead, s.behind) == (1, 1)
    assert "GitHub has 1 commit you don't" in s.summary()


def test_detached(repo):
    (repo / "a.txt").write_text("2\n"); sh(repo, "commit", "-qam", "second")
    sh(repo, "checkout", "-q", "HEAD~1")
    s = git.state(repo)
    assert s.detached and s.branch is None


def test_backup_and_restore(repo):
    (repo / "a.txt").write_text("precious\n")
    b = git.make_backup(repo, "Throw away unsaved changes")
    assert b and b.wip
    assert git.run(["reset", "--hard", "HEAD"], repo).ok
    assert (repo / "a.txt").read_text() == "one\n"
    [found] = git.backups(repo)
    assert found.id == b.id and found.wip
    for st in actions.restore_backup(found).steps:
        assert git.run(st.args, repo).ok, st.args
    assert (repo / "a.txt").read_text() == "precious\n"


def test_no_editor_hang(repo):
    # commit without -m must fail fast instead of opening an editor
    (repo / "a.txt").write_text("x\n"); sh(repo, "add", ".")
    r = git.run(["commit"], repo, timeout=10)
    assert not r.ok and r.code != 124


def test_working_diff_includes_edits_and_new_files(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "new.py").write_text("print('hi')\n")
    d = git.working_diff(git.state(repo))
    assert "-one" in d and "+two" in d
    assert "b/new.py" in d and "+print('hi')" in d


def test_working_diff_hides_secret_files(repo):
    (repo / ".env").write_text("API_KEY=sk-super-secret\n")
    (repo / "config").mkdir()
    (repo / "config" / "id_rsa").write_text("PRIVATE\n")
    d = git.working_diff(git.state(repo))
    assert "sk-super-secret" not in d and "PRIVATE" not in d
    assert d.count("contents not shared") == 2


def test_working_diff_truncates(repo):
    for i in range(5):
        (repo / f"big{i}.txt").write_text("x\n" * 5000)
    d = git.working_diff(git.state(repo), max_chars=5000, per_file=2000)
    assert len(d) < 5000 + 200
    assert "rest of this file's changes left out" in d and "more file(s) left out" in d


def test_working_diff_before_first_commit(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("hello\n")
    sh(tmp_path, "add", "a.txt")
    (tmp_path / "b.txt").write_text("world\n")
    d = git.working_diff(git.state(tmp_path))
    assert "+hello" in d and "+world" in d
