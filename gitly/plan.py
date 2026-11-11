"""Plans: an ordered list of git commands plus plain-English explanations.

Every action in gitly (menus, rescue fixes, AI suggestions) produces a Plan.
The user always sees the Plan before anything runs.

A step is normally a git command. The one exception is a gitly-made step that
adds lines to .gitignore (from the pre-save check); the AI can't produce it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import git, safety


@dataclass
class Step:
    args: list[str]  # without the leading "git"
    why: str
    gitignore: list[str] = field(default_factory=list)  # set: add these lines to .gitignore instead

    @property
    def display(self) -> str:
        if self.gitignore:
            return "add to .gitignore: " + "  ".join(self.gitignore)
        return git.display(self.args)

    @property
    def risk(self) -> str:
        return safety.SAFE if self.gitignore else safety.risk(self.args)

    @property
    def blocked(self) -> str | None:
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
