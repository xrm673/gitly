"""The pre-save check: catch things people regret saving, before "Save my work".

- secret files (.env, private keys, credentials) and API keys pasted into code
- generated folders and OS clutter (node_modules/, .venv/, .DS_Store, ...)
- files too big for GitHub
- conflict markers still in a file when finishing a merge (`leftover_markers`)

Everything here is local pattern matching (no AI), so it is instant and works
without an API key. The fix is a normal Plan the user reviews: add the
new files to .gitignore, leave the rest out of this one save, then commit.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from . import actions, git, secretscan
from .git import RepoState
from .plan import Plan, Step

LARGE_BYTES = 50 * 1024 * 1024  # GitHub warns at 50 MB and rejects files over 100 MB
SCAN_BYTES = 1024 * 1024  # don't read new files bigger than this for keys

# Folders that tools generate and that should never be saved.
JUNK_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".next", ".nuxt", ".turbo", ".parcel-cache", ".gradle", ".svelte-kit",
}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# Characters that mean something special in .gitignore. Paths with them are
# left out with a pathspec instead, so we never ignore more than intended.
_GITIGNORE_SPECIAL = re.compile(r"[*?\[\]!#\\]|^\s|\s$")


@dataclass
class Finding:
    kind: str  # secret_file | secret_text | junk | large
    path: str  # a file, or a folder ending in "/"
    detail: str  # plain English, shown to the user
    gitignore: str | None  # line to add to .gitignore, or None when ignoring can't help

    @property
    def headline(self) -> str:
        return {
            "secret_file": "Secrets file",
            "secret_text": "API key in code",
            "junk": "Generated files",
            "large": "Very large file",
        }[self.kind]


def _junk(path: str) -> tuple[str, str] | None:
    """(what to leave out, .gitignore line) if `path` is generated clutter."""
    parts = path.split("/")
    for i, part in enumerate(parts[:-1]):
        if part in JUNK_DIRS:
            return "/".join(parts[: i + 1]) + "/", part + "/"
    if parts[-1] in JUNK_FILES:
        return path, parts[-1]
    return None


def _ignore_line(path: str) -> str | None:
    return None if _GITIGNORE_SPECIAL.search(path) else path


def _scan_file(root: str, path: str) -> tuple[int, str] | None:
    """(line number, key name) of the first key in a new file, or None."""
    try:
        f = Path(root) / path
        if f.stat().st_size > SCAN_BYTES:
            return None
        data = f.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8000]:
        return None  # binary
    for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if name := secretscan.find(line):
            return n, name
    return None


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _scan_diff(root: str, base: str, skip: set[str]) -> dict[str, tuple[int, str]]:
    """{path: (line number, key name)} for keys in lines added to tracked files."""
    r = git.run(["diff", "--no-ext-diff", "--no-textconv", "-U0", base], root)
    found: dict[str, tuple[int, str]] = {}
    for chunk in git._patch_chunks(r.stdout):
        path = git._chunk_path(chunk)
        if path in skip:
            continue
        line_no = 0
        for line in chunk.splitlines():
            if m := _HUNK.match(line):
                line_no = int(m.group(1))
            elif line.startswith("+") and not line.startswith("+++"):
                if name := secretscan.find(line):
                    found[path] = (line_no, name)
                    break
                line_no += 1
    return found


def check(s: RepoState) -> list[Finding]:
    """Everything worth a warning in what "Save my work" would include."""
    new = set(s.untracked) | {c.path for c in s.changes if c.staged and c.status == "new"}
    saved = new | {c.path for c in s.changes if c.status != "deleted"}
    findings: list[Finding] = []
    seen_junk: set[str] = set()
    flagged: set[str] = set()

    for path in sorted(saved):
        is_new = path in new
        junk = _junk(path) if is_new else None
        if junk:
            flagged.add(path)
            where, line = junk
            if where not in seen_junk:
                seen_junk.add(where)
                findings.append(Finding(
                    "junk", where,
                    "Created automatically by your tools and can be rebuilt any time. "
                    "Saving it bloats your project and causes pointless conflicts.",
                    line))
            continue
        if secretscan.is_secret_file(path):
            flagged.add(path)
            if is_new:
                detail = ("This kind of file usually holds passwords or API keys. If it's saved and "
                          "uploaded, anyone who can see your project can use them.")
            else:
                detail = ("This file was already saved in an earlier commit, so it's in your history. "
                          "Leave the new changes out. If the project is on GitHub, change the keys in it.")
            findings.append(Finding("secret_file", path, detail, _ignore_line(path) if is_new else None))
            continue
        try:
            size = os.path.getsize(Path(s.root) / path)
        except OSError:
            size = 0
        if size > LARGE_BYTES:
            flagged.add(path)
            findings.append(Finding(
                "large", path,
                f"This file is {size / 1024 / 1024:.0f} MB. GitHub rejects files over 100 MB, and big "
                "files make every download of your project slow.",
                _ignore_line(path) if is_new else None))

    key_hits: dict[str, tuple[int, str]] = {}
    for path in sorted(new - flagged):
        if hit := _scan_file(s.root, path):
            key_hits[path] = hit
    if s.head_sha:
        key_hits.update(_scan_diff(s.root, s.head_sha, flagged | new))
    for path, (line_no, name) in sorted(key_hits.items()):
        findings.append(Finding(
            "secret_text", path,
            f"Line {line_no} looks like {name}. Move it into a .env file (which gitly keeps out of "
            "your saves) and read it from there. If it was ever uploaded, replace the key.",
            None))
    return findings


# How git opens and closes a conflict. "=======" alone isn't counted: it is also
# a normal heading underline in Markdown and reStructuredText.
_MARKER = re.compile(rb"(?:<{7}|>{7})(?:\s|$)")


def leftover_markers(s: RepoState) -> list[tuple[str, int]]:
    """(path, line number) of the first conflict marker in each unsaved file."""
    found: list[tuple[str, int]] = []
    for path in sorted(git.unsaved_paths(s)):
        try:
            with open(Path(s.root) / path, "rb") as f:
                if b"\0" in f.read(8000):
                    continue  # binary
                f.seek(0)
                for n, line in enumerate(f, 1):
                    if _MARKER.match(line):
                        found.append((path, n))
                        break
        except OSError:
            continue  # deleted, or a folder
    return found


def without(s: RepoState, findings: list[Finding]) -> RepoState:
    """The repo state as it will look to the save once the findings are left out."""
    out = [f.path for f in findings]

    def kept(path: str) -> bool:
        return not any(git.covers(e, path) for e in out)

    return replace(s, changes=[c for c in s.changes if kept(c.path)],
                   untracked=[u for u in s.untracked if kept(u)], left_out=out)


def save_leaving_out(s: RepoState, findings: list[Finding], message: str) -> Plan:
    """Like "Save my work", minus the flagged items."""
    ignore = list(dict.fromkeys(f.gitignore for f in findings if f.gitignore))
    pathspec_out = [f.path for f in findings if not f.gitignore]
    staged = [f.path for f in findings
              if any(c.staged and git.covers(f.path, c.path) for c in s.changes)]

    steps: list[Step] = []
    if ignore:
        steps.append(Step([], "Tell git to skip these from now on, so they never get saved by accident.",
                          gitignore=ignore))
    add = ["add", "--all"]
    if pathspec_out:
        add += ["--", ".", *(f":(exclude,literal){p}" for p in pathspec_out)]
        why = "Mark every changed file to be included in the save, except the ones left out."
    else:
        why = "Mark every changed file to be included in the save (skipped files stay out)."
    steps.append(Step(add, why))
    if staged:
        unstage = (["restore", "--staged", "--", *staged] if s.head_sha
                   else ["rm", "--cached", "-r", "-q", "--", *staged])
        steps.append(Step(unstage, "Take the left-out files back out of this save. "
                                   "Your files on disk aren't touched."))
    steps.append(actions.save_work(s, message).steps[-1])  # the commit
    n = len(findings)
    return Plan("Save my work", steps,
                summary=f'Save your changes as "{message}", leaving out {n} item{"s" if n != 1 else ""}.')
