"""Plans: an ordered list of git commands plus plain-English explanations.

Every action in gitly (menus, rescue fixes, AI suggestions) produces a Plan.
The user always sees the Plan before anything runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import safety


@dataclass
class Step:
    args: list[str]  # without the leading "git"
    why: str

    @property
    def risk(self) -> str:
        return safety.risk(self.args)

    @property
    def blocked(self) -> str | None:
        return safety.blocked_reason(self.args)


@dataclass
class Plan:
    title: str
    steps: list[Step]
    summary: str = ""
    source: str = "menu"  # menu | rescue | ai
    note: str = ""  # extra text shown above the steps (e.g. AI's answer)

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
