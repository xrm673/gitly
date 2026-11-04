import os
import subprocess
from pathlib import Path

import pytest

# Hermetic git: no user config, fixed identity, default branch "main".
os.environ.update({
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
})


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "work"
    r.mkdir()
    sh(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    sh(r, "add", ".")
    sh(r, "commit", "-qm", "first")
    return r


@pytest.fixture
def remote_pair(tmp_path: Path, repo: Path):
    """repo pushed to a bare 'origin', plus a second clone ('other')."""
    bare = tmp_path / "origin.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    sh(repo, "remote", "add", "origin", str(bare))
    sh(repo, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", str(bare), str(other))
    return repo, other
