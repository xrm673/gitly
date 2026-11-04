import pytest

from gitly.safety import CAUTION, DANGER, SAFE, blocked_reason, risk


@pytest.mark.parametrize("args,expected", [
    (["push"], SAFE),
    (["push", "--force"], DANGER),
    (["push", "--force-with-lease"], DANGER),
    (["push", "origin", "+main"], DANGER),
    (["push", "origin", ":old"], DANGER),
    (["reset", "--soft", "HEAD~1"], CAUTION),
    (["reset", "--hard", "HEAD"], DANGER),
    (["restore", "--staged", "."], SAFE),
    (["restore", "app.py"], DANGER),
    (["checkout", "main"], SAFE),
    (["checkout", "--", "app.py"], DANGER),
    (["branch", "-D", "x"], DANGER),
    (["branch", "--delete", "x"], CAUTION),
    (["clean", "-fd"], DANGER),
    (["stash", "drop"], DANGER),
    (["commit", "-m", "x"], SAFE),
    (["commit", "--amend", "-m", "x"], CAUTION),
    (["pull", "--no-rebase"], CAUTION),
])
def test_risk(args, expected):
    assert risk(args) == expected


@pytest.mark.parametrize("args", [
    ["-c", "core.sshCommand=evil", "push"],
    ["-C", "/tmp", "status"],
    ["config", "core.pager", "evil"],
    ["config", "alias.x", "!rm -rf ~"],
    ["rebase", "-i", "HEAD~3"],
    ["rebase", "-x", "make", "main"],
    ["rebase", "--exec=make", "main"],
    ["add", "-p"],
    ["commit", "--edit"],
    ["push", "--receive-pack=evil", "origin"],
    ["fetch", "--upload-pack=evil"],
    ["filter-branch"],
    ["gc", "--prune=now"],
    ["update-ref", "-d", "refs/heads/main"],
    ["remote", "remove", "origin"],
    ["commit", "-m", "line1\nline2"],
    [],
])
def test_blocked(args):
    assert blocked_reason(args)


@pytest.mark.parametrize("args", [
    ["status"],
    ["log", "-p"],
    ["commit", "-m", "x"],
    ["config", "user.name", "Ada"],
    ["config", "--global", "user.email", "a@b.c"],
    ["update-ref", "refs/gitly/backup/x/head", "abc"],
    ["remote", "add", "origin", "https://github.com/a/b.git"],
    ["switch", "--create", "feature"],
])
def test_allowed(args):
    assert blocked_reason(args) is None
