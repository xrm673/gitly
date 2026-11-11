"""Thin wrapper around the real `git` CLI.

We shell out to git (instead of using a library) so that the command we show
the user is exactly the command that runs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Never let git open an editor, pager, or password prompt inside the TUI:
# any of those would hang the app. Failures surface as stderr we can rescue.
GIT_OVERRIDES = {
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",  # stable, English messages so rescue patterns match
}


@dataclass
class Result:
    args: list[str]
    code: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


def display(args: list[str]) -> str:
    """How a command is shown to the user: `git commit -m "fix login"`."""
    return "git " + shlex.join(args)


def run(args: list[str], cwd: str | Path, timeout: float = 120) -> Result:
    start = time.monotonic()
    try:
        p = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=cwd,
            env={**os.environ, **GIT_OVERRIDES},
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", f"git {args[0]} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        code, out, err = 127, "", "git is not installed or not on PATH"
    return Result(args, code, out, err, time.monotonic() - start)


@dataclass
class FileChange:
    path: str
    staged: bool
    status: str  # human word: modified, new, deleted, renamed, conflict


@dataclass
class RepoState:
    root: str
    branch: str | None  # None when detached
    head_sha: str | None  # None before the first commit
    upstream: str | None
    ahead: int = 0
    behind: int = 0
    changes: list[FileChange] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    merging: bool = False
    rebasing: bool = False
    remotes: list[str] = field(default_factory=list)

    @property
    def detached(self) -> bool:
        return self.branch is None and self.head_sha is not None

    @property
    def dirty(self) -> bool:
        return bool(self.changes or self.untracked or self.conflicts)

    @property
    def staged(self) -> list[FileChange]:
        return [c for c in self.changes if c.staged]

    def summary(self) -> str:
        """Plain-English one-liner for the header."""
        parts = []
        if self.merging:
            parts.append("You're in the middle of combining branches (merge).")
        elif self.rebasing:
            parts.append("You're in the middle of a rebase.")
        if self.detached:
            parts.append(f"You're not on a branch (detached at {self.head_sha[:7]}).")
        elif self.branch:
            parts.append(f"You're on [b]{self.branch}[/b].")
        if self.head_sha is None:
            parts.append("No commits yet.")
        n = len({c.path for c in self.changes}) + len(self.untracked)
        if n:
            parts.append(f"{n} file{'s' if n != 1 else ''} changed, not saved yet.")
        elif self.head_sha:
            parts.append("Everything is saved.")
        if not self.remotes:
            parts.append("Not connected to GitHub (no remote).")
        elif self.branch and not self.upstream:
            parts.append("This branch has never been uploaded.")
        else:
            if self.behind:
                parts.append(f"GitHub has {self.behind} commit{'s' if self.behind != 1 else ''} you don't.")
            if self.ahead:
                parts.append(f"{self.ahead} commit{'s' if self.ahead != 1 else ''} not uploaded yet.")
            if self.upstream and not self.ahead and not self.behind:
                parts.append("In sync with GitHub.")
        return " ".join(parts)

    def as_context(self) -> str:
        """Compact description of the repo for the AI prompt."""
        lines = [
            f"branch: {self.branch or '(detached HEAD)'}",
            f"head: {self.head_sha or '(no commits yet)'}",
            f"upstream: {self.upstream or '(none)'} ahead={self.ahead} behind={self.behind}",
            f"remotes: {', '.join(self.remotes) or '(none)'}",
            f"merge in progress: {self.merging}; rebase in progress: {self.rebasing}",
        ]
        for c in self.changes[:50]:
            lines.append(f"  {'staged' if c.staged else 'unstaged'} {c.status}: {c.path}")
        for u in self.untracked[:50]:
            lines.append(f"  untracked: {u}")
        for c in self.conflicts:
            lines.append(f"  CONFLICT: {c}")
        return "\n".join(lines)


_XY = {"M": "modified", "T": "modified", "A": "new", "D": "deleted", "R": "renamed", "C": "copied"}


def find_root(cwd: str | Path) -> str | None:
    r = run(["rev-parse", "--show-toplevel"], cwd)
    return r.stdout.strip() if r.ok else None


def parse_status(text: str) -> dict:
    """Parse `git status --porcelain=v2 --branch -z`."""
    out: dict = {"branch": None, "oid": None, "upstream": None, "ahead": 0, "behind": 0,
                 "changes": [], "untracked": [], "conflicts": []}
    entries = text.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if not e:
            continue
        if e.startswith("# branch.oid "):
            oid = e.split(" ", 2)[2]
            out["oid"] = None if oid == "(initial)" else oid
        elif e.startswith("# branch.head "):
            head = e.split(" ", 2)[2]
            out["branch"] = None if head == "(detached)" else head
        elif e.startswith("# branch.upstream "):
            out["upstream"] = e.split(" ", 2)[2]
        elif e.startswith("# branch.ab "):
            _, _, a, b = e.split(" ")
            out["ahead"], out["behind"] = int(a), -int(b)
        elif e[0] in "12":
            fields = e.split(" ", 8 if e[0] == "1" else 9)
            xy, path = fields[1], fields[-1]
            if e[0] == "2":
                i += 1  # skip the original path that follows a rename
            x, y = xy[0], xy[1]
            if x != ".":
                out["changes"].append(FileChange(path, True, _XY.get(x, "changed")))
            if y != ".":
                out["changes"].append(FileChange(path, False, _XY.get(y, "changed")))
        elif e[0] == "u":
            out["conflicts"].append(e.split(" ", 10)[-1])
        elif e[0] == "?":
            out["untracked"].append(e[2:])
    return out


def state(cwd: str | Path) -> RepoState | None:
    root = find_root(cwd)
    if root is None:
        return None
    r = run(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"], root)
    s = parse_status(r.stdout)
    gitdir = Path(run(["rev-parse", "--absolute-git-dir"], root).stdout.strip())
    remotes = run(["remote"], root).stdout.split()
    return RepoState(
        root=root,
        branch=s["branch"],
        head_sha=s["oid"],
        upstream=s["upstream"],
        ahead=s["ahead"],
        behind=s["behind"],
        changes=s["changes"],
        untracked=s["untracked"],
        conflicts=s["conflicts"],
        merging=(gitdir / "MERGE_HEAD").exists(),
        rebasing=(gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists(),
        remotes=remotes,
    )


def branches(cwd: str | Path) -> list[str]:
    r = run(["branch", "--format=%(refname:short)"], cwd)
    return [b for b in r.stdout.splitlines() if b]


def recent_log(cwd: str | Path, n: int = 20) -> list[str]:
    r = run(["log", f"-{n}", "--format=%h  %ar  %an  %s"], cwd)
    return r.stdout.splitlines() if r.ok else []


# Files whose contents must never leave the machine. The AI sees only their names.
SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx|keystore)|id_(rsa|dsa|ecdsa|ed25519)"
    r"|\.npmrc|\.pypirc|\.netrc|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$",
    re.IGNORECASE,
)


def _patch_chunks(patch: str) -> list[str]:
    return [c for c in re.split(r"(?m)^(?=diff --git )", patch) if c.startswith("diff --git ")]


def _chunk_path(chunk: str) -> str:
    header = chunk.split("\n", 1)[0]
    return header.rsplit(" b/", 1)[-1].strip('"')


def working_diff(s: RepoState, max_chars: int = 12000, per_file: int = 3000) -> str:
    """What a save would include (tracked edits + new files) as a patch, for the AI.

    Contents of secret-looking files are replaced by a one-line note, each file
    is capped at `per_file` characters, and the whole thing at `max_chars`.
    """
    base = s.head_sha or run(["hash-object", "-t", "tree", "/dev/null"], s.root).stdout.strip()
    chunks = _patch_chunks(run(["diff", "--no-ext-diff", "--no-textconv", "-M", base], s.root).stdout)
    for path in s.untracked:
        if len(chunks) >= 60:
            break
        # --no-index exits 1 when the files differ, which is always the case here
        r = run(["diff", "--no-ext-diff", "--no-textconv", "--no-index", "--", "/dev/null", path], s.root)
        chunks += _patch_chunks(r.stdout)

    out: list[str] = []
    used = 0
    for i, chunk in enumerate(chunks):
        path = _chunk_path(chunk)
        if SENSITIVE.search(path):
            chunk = f"diff --git a/{path} b/{path}\n[contents not shared: looks like a secrets file]\n"
        elif len(chunk) > per_file:
            chunk = chunk[:per_file] + "\n[... rest of this file's changes left out]\n"
        if used + len(chunk) > max_chars:
            out.append(f"[... {len(chunks) - i} more file(s) left out to keep this short]")
            break
        out.append(chunk)
        used += len(chunk)
    return "".join(out).strip()


BACKUP_NS = "refs/gitly/backup"


@dataclass
class Backup:
    id: str  # e.g. 20261104-105400_discard-changes
    head: str  # ref to the commit HEAD was on
    wip: str | None  # ref to a stash-style commit holding uncommitted work

    @property
    def label(self) -> str:
        stamp, _, what = self.id.partition("_")
        d, _, t = stamp.partition("-")
        when = f"{d[4:6]}/{d[6:8]} {t[:2]}:{t[2:4]}" if len(d) == 8 and len(t) >= 4 else stamp
        extra = " (+ unsaved changes)" if self.wip else ""
        return f"{when}, before \"{what.replace('-', ' ')}\"{extra}"


def make_backup(cwd: str | Path, label: str) -> Backup | None:
    """Snapshot HEAD and any uncommitted work under refs/gitly/backup/<id>/.

    Uses `git stash create`, which records the working tree as a commit
    without touching files or the stash list.
    """
    head = run(["rev-parse", "--verify", "-q", "HEAD"], cwd).stdout.strip()
    if not head:
        return None
    slug = "".join(ch if ch.isalnum() else "-" for ch in label.lower()).strip("-")[:40] or "change"
    bid = f"{time.strftime('%Y%m%d-%H%M%S')}_{slug}"
    base = f"{BACKUP_NS}/{bid}"
    run(["update-ref", f"{base}/head", head], cwd)
    wip = run(["stash", "create", f"gitly backup: {label}"], cwd).stdout.strip()
    if wip:
        run(["update-ref", f"{base}/wip", wip], cwd)
    return Backup(bid, f"{base}/head", f"{base}/wip" if wip else None)


def backups(cwd: str | Path) -> list[Backup]:
    r = run(["for-each-ref", BACKUP_NS, "--format=%(refname)"], cwd)
    found: dict[str, Backup] = {}
    for ref in r.stdout.split():
        rest = ref[len(BACKUP_NS) + 1:]
        bid, _, kind = rest.rpartition("/")
        b = found.setdefault(bid, Backup(bid, f"{BACKUP_NS}/{bid}/head", None))
        if kind == "wip":
            b.wip = ref
    return sorted(found.values(), key=lambda b: b.id, reverse=True)
