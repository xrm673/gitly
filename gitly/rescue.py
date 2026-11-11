"""Turn common git failures into plain-English explanations and one-click fixes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import actions
from .git import RepoState, Result
from .plan import Plan, Step


@dataclass
class Rescue:
    title: str
    explanation: str
    fixes: list[Plan] = field(default_factory=list)
    needs_input: str | None = None  # e.g. "remote_url", "identity"


Rule = tuple[re.Pattern, Callable[[Result, RepoState], Rescue]]


def _push_rejected(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "GitHub has changes you don't have yet",
        "Someone (maybe you, on another computer or on github.com) uploaded commits "
        "to this branch. Git won't overwrite them. Get their changes first, then upload again.",
        [Plan("Get latest, then upload", [
            Step(["pull", "--no-rebase"], "Download and combine the new commits from GitHub."),
            Step(["push"], "Upload again."),
        ], source="rescue")],
    )


def _no_upstream(r: Result, s: RepoState) -> Rescue:
    remote = "origin" if "origin" in s.remotes else (s.remotes[0] if s.remotes else "origin")
    return Rescue(
        "This branch has never been uploaded",
        "Git doesn't know where on GitHub this branch should go yet. "
        "Uploading it once with --set-upstream fixes that for good.",
        [Plan("Upload this branch", [Step(
            ["push", "--set-upstream", remote, s.branch or "HEAD"],
            "Upload and remember the destination.")], source="rescue")],
    )


def _conflict(r: Result, s: RepoState) -> Rescue:
    files = ", ".join(s.conflicts) or "some files"
    return Rescue(
        "Your changes and GitHub's changes overlap",
        f"Both sides edited the same lines in: {files}. Git marked the spots with "
        "<<<<<<< and >>>>>>>. Open those files (or ask your AI editor), keep the "
        "right version, delete the markers, then come back and finish.",
        [actions.finish_merge(s), actions.abort_merge(s)],
    )


def _divergent(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "You and GitHub both have new commits",
        "Git needs to know how to combine them. The simplest option is a merge, "
        "which keeps everyone's work.",
        [Plan("Combine with a merge", [Step(["pull", "--no-rebase"], "Download and merge.")], source="rescue")],
    )


def _would_overwrite(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "Your unsaved changes are in the way",
        "Doing this would overwrite edits you haven't saved. Either save them as a "
        "commit, or tuck them away temporarily (stash) and bring them back after.",
        [
            Plan("Tuck changes away, retry, bring them back", [
                Step(["stash", "push", "--include-untracked"], "Temporarily put your changes aside."),
                Step(r.args, "Retry what you were doing."),
                Step(["stash", "pop"], "Bring your changes back."),
            ], source="rescue"),
        ],
    )


def _nothing_to_commit(r: Result, s: RepoState) -> Rescue:
    return Rescue("Nothing to save", "There are no changes since your last save. You're all set.")


def _no_remote(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "Not connected to GitHub",
        "This project doesn't have a GitHub address yet. Create an empty repository "
        "on github.com and paste its URL to connect.",
        needs_input="remote_url",
    )


def _auth(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "GitHub didn't recognize you",
        "Your computer isn't logged in to GitHub (or doesn't have access to this repo). "
        "The easiest fix: install the GitHub CLI and run `gh auth login` in a normal "
        "terminal once, then try again.",
    )


def _identity(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "Git needs your name and email",
        "Every save is labeled with who made it. Set this once and you're done.",
        needs_input="identity",
    )


def _no_commits(r: Result, s: RepoState) -> Rescue:
    return Rescue(
        "Nothing saved yet",
        "This project has no commits yet, so there's nothing to upload or undo. Save your work first.",
    )


RULES: list[Rule] = [
    (re.compile(r"Please tell me who you are|unable to auto-detect email"), _identity),
    (re.compile(r"CONFLICT|Automatic merge failed|unmerged files|you need to resolve your current index"), _conflict),
    (re.compile(r"has no upstream branch|no tracking information"), _no_upstream),
    (re.compile(r"\[rejected\].*\((fetch first|non-fast-forward)\)|Updates were rejected"), _push_rejected),
    (re.compile(r"divergent branches|Need to specify how to reconcile"), _divergent),
    (re.compile(r"would be overwritten|Please commit your changes or stash them"), _would_overwrite),
    (re.compile(r"nothing to commit|no changes added to commit"), _nothing_to_commit),
    (re.compile(r"No configured push destination|does not appear to be a git repository|No remote repository specified"), _no_remote),
    (re.compile(r"Authentication failed|Permission denied \(publickey\)|could not read Username|Repository not found|terminal prompts disabled"), _auth),
    (re.compile(r"src refspec .* does not match any|ambiguous argument 'HEAD|does not have any commits yet"), _no_commits),
]


def diagnose(r: Result, s: RepoState) -> Rescue | None:
    text = r.output
    for pattern, build in RULES:
        if pattern.search(text):
            return build(r, s)
    return None


def identity_plan(name: str, email: str) -> Plan:
    return Plan("Set your name and email", [
        Step(["config", "user.name", name], "Label your saves with your name (this project only)."),
        Step(["config", "user.email", email], "And your email."),
    ], source="rescue")
