"""The next step: one suggestion at a time, like autocomplete for git.

rules() works out the obvious next step from the repo state alone: instant,
no API key. When a key is set, from_ai() lets the AI read the unsaved changes
and refine it: write the description ahead of time, split unrelated changes
into separate saves, start a branch for work that doesn't belong here, or say
whether a branch looks finished.

The AI never writes git commands here. It picks one action from a short list
that gitly computed, and gitly builds the plan, so a suggestion goes through
the same review screen and safety checks as everything else. ghost() turns a
suggestion into what it would add to the branch map.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import ai, git
from .git import RepoState
from .graph import Ghost, Graph, Lane


@dataclass
class SaveGroup:
    message: str
    files: list[str]


@dataclass
class Suggestion:
    kind: str  # save | split_save | new_branch | upload | pull | merge | cleanup | menu | none
    title: str  # what to do, short and imperative
    reason: str  # one sentence: why this, why now
    source: str = "rules"  # rules | ai
    menu: str | None = None  # the main-menu action this stands for (kind "menu", upload, pull)
    message: str = ""  # save: a description written ahead of time
    groups: list[SaveGroup] = field(default_factory=list)  # split_save
    branch: str = ""  # new_branch: its name; cleanup: the branch to delete; merge: the source
    target: str = ""  # merge: the branch receiving the work

    @property
    def acceptable(self) -> bool:
        return self.kind != "none"

    @property
    def key(self) -> str:
        """Identifies the suggestion, so one the user dismissed stays dismissed."""
        return f"{self.kind}:{self.menu or ''}:{self.branch}:{self.target}"


def _files(n: int) -> str:
    return f"{n} file{'s' if n != 1 else ''}"


def _saves(n: int) -> str:
    return f"{n} save{'s' if n != 1 else ''}"


def unsaved_count(s: RepoState) -> int:
    return len(git.unsaved_paths(s))


def _current(g: Graph | None) -> Lane | None:
    return next((l for l in g.lanes if l.current), None) if g else None


def rules(s: RepoState | None, g: Graph | None) -> Suggestion:
    """The obvious next step, from the repo state alone."""
    if s is None:
        return Suggestion("menu", "Start tracking this folder", menu="init",
                          reason="gitly can only save your work once this folder is a git project.")
    if s.conflicts:
        n = len(s.conflicts)
        return Suggestion("menu", f"Sort out the conflicts in {_files(n)}", menu="resolve",
                          reason="See both versions of each spot side by side, with the AI's "
                                 "take on which to keep. You make the call.")
    if s.merging or s.rebasing or s.reverting:
        return Suggestion("menu", "Finish what's in progress", menu="finish_merge",
                          reason="The conflicts are sorted out. One more save completes it.")
    if s.detached:
        return Suggestion("menu", "Make a branch here", menu="branch_here",
                          reason="You're looking at an old save. Saves made here can get lost without a branch.")
    n = unsaved_count(s)
    if n:
        if s.head_sha is None:
            return Suggestion("save", "Make your first save",
                              f"{_files(n)} in this folder {'is' if n == 1 else 'are'}n't saved yet. "
                              "A save is a snapshot you can always come back to.")
        return Suggestion("save", "Save your work",
                          f"You changed {_files(n)} since your last save.")
    if s.head_sha is None:
        return Suggestion("none", "Nothing to save yet",
                          "Add or edit a file in this folder, then come back to save it.")
    if s.behind:
        return Suggestion("pull", "Get the latest from GitHub", menu="pull",
                          reason=f"GitHub has {_saves(s.behind)} you don't have yet"
                                 + (", and git wants those before you upload yours." if s.ahead else "."))
    if s.remotes and s.branch and not s.upstream:
        return Suggestion("upload", f"Upload {s.branch} to GitHub", menu="upload",
                          reason="This branch only exists on this computer. Uploading backs it up.")
    if s.remotes and s.ahead:
        return Suggestion("upload", "Upload to GitHub", menu="upload",
                          reason=f"{_saves(s.ahead)} {'is' if s.ahead == 1 else 'are'} only on this computer.")
    lane = _current(g)
    if g and g.trunk and lane and lane.kind == "branch" and lane.commits:
        return Suggestion("merge", f"Combine {lane.name} into {g.trunk}", branch=lane.name, target=g.trunk,
                          reason=f"Everything here is saved{' and uploaded' if s.upstream else ''}. "
                                 f"When this work is done, bring it into {g.trunk}.")
    if g and lane and lane.kind == "trunk":
        done = next((l for l in g.lanes if l.kind == "branch" and l.merged), None)
        if done:
            return Suggestion("cleanup", f"Delete {done.name}", branch=done.name,
                              reason=f"Its work is already in {g.trunk}, so the branch isn't needed anymore.")
    if not s.remotes:
        return Suggestion("menu", "Connect to GitHub", menu="connect",
                          reason="Your saves only live on this computer. GitHub keeps a backup online.")
    return Suggestion("none", "You're all caught up",
                      "Everything is saved and in sync. Edit your files and come back to save.")


def ghost(sg: Suggestion, s: RepoState | None) -> Ghost | None:
    """What accepting `sg` would add to the branch map."""
    if s is None:
        return None
    if sg.kind == "save":
        return Ghost(saves=1)
    if sg.kind == "split_save":
        return Ghost(saves=len(sg.groups))
    if sg.kind == "new_branch":
        return Ghost(branch=sg.branch, saves=1 if s.dirty else 0)
    if sg.kind == "merge":
        return Ghost(merge=(sg.branch, sg.target))
    if sg.kind == "upload" and s.branch:
        return Ghost(tails={s.branch: "⇡ upload"})
    if sg.kind == "pull" and s.branch:
        return Ghost(tails={s.branch: "⇣ get latest"})
    if sg.kind == "cleanup":
        return Ghost(tails={sg.branch: "✕ delete"})
    return None


# ----------------------------------------------------------------- the AI's turn

def ambient() -> bool:
    """True when the AI may look at the repo unprompted (GITLY_AI_SUGGEST=0 turns it off)."""
    off = os.environ.get("GITLY_AI_SUGGEST", "1").strip().lower() in ("0", "false", "no", "off")
    return not off and ai.available()


def allowed(s: RepoState | None, base: Suggestion) -> list[str]:
    """The actions the AI may choose from right now. Empty: the rules are enough."""
    if s is None or s.conflicts or s.merging or s.rebasing or s.reverting or s.detached:
        return []
    if base.kind == "save":
        out = ["save"]
        plain = all(c.status not in ("renamed", "copied") for c in s.changes)
        if s.head_sha and plain and unsaved_count(s) > 1:
            out.append("split_save")
        if s.head_sha and s.branch:
            out.append("new_branch")
        return out
    if base.kind == "merge":
        return ["combine", "nothing"]
    return []


def signature(s: RepoState | None) -> str:
    """Changes whenever the repo or an unsaved file does. Cheap: no diff, just file stats."""
    if s is None:
        return ""
    parts = [s.head_sha or "", s.branch or "", s.upstream or "", str(s.ahead), str(s.behind),
             str(s.merging), str(s.rebasing), str(s.reverting), *sorted(s.conflicts)]
    for path in sorted(git.unsaved_paths(s)):
        try:
            st = (Path(s.root) / path).stat()
            parts.append(f"{path}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{path}:gone")
    return "\n".join(parts)


def _facts(s: RepoState, g: Graph | None, base: Suggestion, actions: list[str]) -> str:
    out = ""
    if g:
        rows = []
        for l in g.lanes:
            if not l.is_branch:
                continue
            notes = [("main branch" if l.kind == "trunk" else
                      f"already combined into {g.trunk}" if l.merged else f"{len(l.commits)} saves of its own")]
            if l.current:
                notes.append("you are here")
            rows.append(f"{l.name}: {', '.join(notes)}")
        out += "<branches>\n" + "\n".join(rows) + "\n</branches>\n"
    if base.kind == "merge":
        log = git.run(["log", "--format=%s", "--max-count=30", f"{base.target}..{base.branch}"], s.root)
        stat = git.run(["diff", "--stat=100", f"{base.target}...{base.branch}"], s.root)
        out += (f"<work_on_this_branch>\nsaves, newest first:\n{log.stdout.strip()}\n\n"
                f"files changed compared to {base.target}:\n{stat.stdout.strip()[-3000:]}\n</work_on_this_branch>\n")
    out += f"<allowed_actions>\n{', '.join(actions)}\n</allowed_actions>"
    return out


def from_ai(s: RepoState, g: Graph | None, base: Suggestion) -> tuple[Suggestion, dict]:
    """Ask the AI to refine `base`. Returns (suggestion, telemetry); raises ai.AIUnavailable.

    Anything the AI gets wrong (an action that isn't allowed, a file list that
    doesn't match, a bad branch name) falls back to `base`.
    """
    actions = allowed(s, base)
    out, tel = ai.suggest_next(s, _facts(s, g, base, actions), with_changes=base.kind == "save")
    sg = _checked(out, s, base, actions)
    tel.update(action=out.action, used=sg is not base, step=sg.kind)
    return sg, tel


def _clean(text: str, limit: int) -> str:
    lines = text.strip().splitlines()
    return (lines[0] if lines else "").strip().strip('"\'`').strip()[:limit]


def _checked(out: ai.NextStep, s: RepoState, base: Suggestion, actions: list[str]) -> Suggestion:
    if out.action not in actions:
        return base
    reason = _clean(out.reason, 200) or base.reason
    message = _clean(out.message, 72)
    if out.action == "save" and message:
        return Suggestion("save", f"Save your work: “{message}”", reason, source="ai", message=message)
    if out.action == "split_save":
        groups = [SaveGroup(_clean(grp.message, 72), list(grp.files)) for grp in out.groups]
        listed = [f for grp in groups for f in grp.files]
        if (2 <= len(groups) <= 3 and all(grp.message and grp.files for grp in groups)
                and len(listed) == len(set(listed)) and set(listed) == git.unsaved_paths(s)):
            return Suggestion("split_save", f"Save as {len(groups)} separate saves", reason,
                              source="ai", groups=groups)
        return base
    if out.action == "new_branch":
        name = git.tidy_branch_name(out.branch)
        if name and name not in git.branches(s.root) and git.valid_branch_name(s.root, name):
            return Suggestion("new_branch", f"Start a branch for this: {name}", reason,
                              source="ai", branch=name)
        return base
    if out.action == "combine" and base.kind == "merge":
        return Suggestion("merge", base.title, reason, source="ai", branch=base.branch, target=base.target)
    if out.action == "nothing" and base.kind == "merge":
        return Suggestion("none", f"Keep going on {base.branch}", reason, source="ai")
    return base
