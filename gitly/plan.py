"""Plans: an ordered list of git commands plus plain-English explanations.

Every action in gitly (menus, rescue fixes, AI suggestions) produces a Plan.
The user always sees the Plan before anything runs.

A step is normally a git command. The exceptions are gitly-made steps that add
lines to .gitignore (from the pre-save check) or write a file whose conflicts
the user sorted out (see conflicts.py). The AI can't produce either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import git, safety


@dataclass
class Step:
    args: list[str]  # without the leading "git"
    why: str
    gitignore: list[str] = field(default_factory=list)  # set: add these lines to .gitignore instead
    # set: write these bytes to `path` instead. `expect` is the file as gitly read it;
    # if it changed since then, nothing is written.
    write: bytes | None = None
    path: str = ""
    expect: bytes | None = None

    @property
    def display(self) -> str:
        if self.write is not None:
            return f"write {self.path}"
        if self.gitignore:
            return "add to .gitignore: " + "  ".join(self.gitignore)
        return git.display(self.args)

    @property
    def risk(self) -> str:
        return safety.SAFE if self.gitignore or self.write is not None else safety.risk(self.args)

    @property
    def blocked(self) -> str | None:
        if self.write is not None:
            p = PurePosixPath(self.path)
            ok = self.path and not p.is_absolute() and ".." not in p.parts and "\0" not in self.path
            return None if ok else "gitly only writes files inside this project"
        if self.gitignore:
            bad = [e for e in self.gitignore if not e.strip() or "\n" in e or "\r" in e]
            return "a .gitignore line can't be empty or contain newlines" if bad else None
        return safety.blocked_reason(self.args)


@dataclass
class Plan:
    title: str
    steps: list[Step]
    summary: str = ""
    source: str = "menu"  # menu | rescue | ai
    note: str = ""  # extra text shown above the steps (e.g. AI's answer)
    preview: tuple[str, str] | None = None  # (now, after) branch map markup

    @property
    def risk(self) -> str:
        if not self.steps:
            return safety.SAFE
        return max((s.risk for s in self.steps), key=safety.RISK_ORDER.index)

    @property
    def blocked(self) -> list[tuple[Step, str]]:
        return [(s, r) for s in self.steps if (r := s.blocked)]

    @property
    def runnable(self) -> bool:
        return bool(self.steps) and not self.blocked


@dataclass
class Choice:
    """A menu option that leads to a Plan (possibly after asking for input)."""
    label: str
    key: str
    hint: str = ""
    data: dict = field(default_factory=dict)
