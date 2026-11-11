"""Deterministic (no-AI) plans for the main menu."""

from __future__ import annotations

import time

from .git import Backup, RepoState
from .plan import Plan, Step


def save_work(s: RepoState, message: str) -> Plan:
    steps = [
        Step(["add", "--all"], "Mark every changed file to be included in the save."),
        Step(["commit", "-m", message], "Save a snapshot (a \"commit\") with your description."),
    ]
    return Plan("Save my work", steps, summary=f'Save all changes as "{message}".')


def split_save(s: RepoState, groups: list[tuple[str, list[str]]], note: str = "",
               preview: tuple[str, str] | None = None) -> Plan:
    """Save unrelated changes as separate saves. groups: (description, files), in order.

    The last save takes everything that's left, so nothing stays unsaved.
    """
    steps = []
    if s.staged:
        steps.append(Step(["restore", "--staged", "."],
                          "Unmark files that were already marked for saving, so each save gets only "
                          "its own files. Your edits stay exactly as they are."))
    for i, (message, files) in enumerate(groups):
        if i == len(groups) - 1:
            steps.append(Step(["add", "--all"], "Mark everything that's left for the last save: "
                              + ", ".join(files[:4]) + (" …" if len(files) > 4 else "") + "."))
        else:
            steps.append(Step(["add", "--", *files],
                              f"Mark only {'this file' if len(files) == 1 else f'these {len(files)} files'} "
                              "for the next save."))
        steps.append(Step(["commit", "-m", message], f"Save {'them' if len(files) != 1 else 'it'} as one snapshot."))
    return Plan("Save as separate saves", steps, source="ai", note=note, preview=preview,
                summary=f"Your changes become {len(groups)} saves instead of one, so each can be "
                        "understood (and undone) by itself.")


def upload(s: RepoState) -> Plan:
    if not s.remotes:
        return Plan("Upload to GitHub", [], note=(
            "This project isn't connected to GitHub yet. Create an empty repo on "
            "github.com, then choose [b]Connect to GitHub[/b] from the menu."))
    if s.branch and not s.upstream:
        remote = "origin" if "origin" in s.remotes else s.remotes[0]
        return Plan("Upload to GitHub", [Step(
            ["push", "--set-upstream", remote, s.branch],
            f"Upload '{s.branch}' for the first time and remember where it goes, "
            f"so next time a plain push works.")],
            summary="First upload of this branch.")
    note = ""
    if s.dirty:
        note = "Heads up: you have unsaved changes. Upload only sends saved commits; use Save first if you want them included."
    return Plan("Upload to GitHub", [Step(["push"], "Send your saved commits to GitHub.")],
                summary=f"Upload {s.ahead} commit(s)." if s.ahead else "Upload.", note=note)


def get_latest(s: RepoState) -> Plan:
    return Plan("Get latest", [Step(
        ["pull", "--no-rebase"],
        "Download new commits from GitHub and combine them with yours.")],
        summary="Download and combine changes from GitHub.")


def new_branch(name: str) -> Plan:
    return Plan("New branch", [Step(
        ["switch", "--create", name],
        f"Make a new branch called '{name}' starting from here and move onto it. "
        "Your unsaved changes come with you.")])


def switch_branch(name: str) -> Plan:
    return Plan("Switch branch", [Step(["switch", name], f"Move to the '{name}' branch.")])


def from_map(title: str, steps: list[Step], summary: str = "", note: str = "",
             preview: tuple[str, str] | None = None, save_first: Plan | None = None) -> Plan:
    """A branch-map action, optionally preceded by saving unsaved changes."""
    if save_first is not None:
        steps = save_first.steps + steps
        summary = f"{save_first.summary} Then: {summary[0].lower() + summary[1:]}" if summary else save_first.summary
    return Plan(title, steps, summary=summary, note=note, source="map", preview=preview)


def switch_steps(name: str, carrying: int) -> list[Step]:
    why = f"Move to the '{name}' branch."
    if carrying:
        why += (f" Your {carrying} unsaved file{'s come' if carrying != 1 else ' comes'} with you: "
                f"'{name}' doesn't change {'them' if carrying != 1 else 'it'}.")
    return [Step(["switch", name], why)]


def branch_steps(name: str, at: str | None, label: str, carrying: int) -> list[Step]:
    """Create `name` at commit `at` (None = where you are) and move onto it."""
    args = ["switch", "--create", name] + ([at] if at else [])
    why = f"Make a new branch called '{name}' starting from {label} and move onto it."
    if carrying:
        why += " Your unsaved changes come with you."
    return [Step(args, why)]


def merge_steps(source: str, target: str, current: str | None) -> list[Step]:
    steps = []
    if current != target:
        steps.append(Step(["switch", target], f"Move to '{target}', the branch receiving the work."))
    steps.append(Step(["merge", "--no-ff", "--no-edit", source],
                      f"Combine '{source}' into '{target}' as one new save. "
                      f"'{source}' itself doesn't change, and you can undo the whole thing later."))
    return steps


def uncommit_steps() -> list[Step]:
    return [Step(["reset", "--soft", "HEAD~1"],
                 "Un-save your latest save. The changes in it go back to being unsaved, "
                 "exactly as they were, so you can edit them or save again.")]


def revert_steps(sha: str, subject: str, merge: bool) -> list[Step]:
    if merge:
        return [Step(["revert", "--no-edit", "-m", "1", sha[:12]],
                     f"Add a new save that takes out everything “{subject}” brought in. "
                     "“-m 1” means: keep the branch you merged into, drop what came from the other one.")]
    return [Step(["revert", "--no-edit", sha[:12]],
                 f"Add a new save that does the exact opposite of “{subject}”. "
                 "The original stays in your history, so nothing is lost.")]


def delete_branch(name: str) -> Plan:
    return Plan("Delete branch", [Step(
        ["branch", "--delete", name],
        f"Delete the local branch '{name}'. Git refuses if it has work that "
        "isn't merged anywhere, so nothing is lost.")])


def undo_last_commit() -> Plan:
    return Plan("Undo last commit", [Step(
        ["reset", "--soft", "HEAD~1"],
        "Un-save the most recent commit. Your file changes stay exactly as they are.")],
        summary="Your work is kept; only the snapshot is removed.")


def unstage_all() -> Plan:
    return Plan("Unstage everything", [Step(
        ["restore", "--staged", "."],
        "Un-mark files for the next save. Your edits are untouched.")])


def discard_changes() -> Plan:
    return Plan("Throw away unsaved changes", [Step(
        ["reset", "--hard", "HEAD"],
        "Put every tracked file back to how it was at the last save. "
        "New (untracked) files are left alone.")],
        summary="gitly saves a backup first so you can get these changes back.")


def restore_backup(b: Backup) -> Plan:
    steps = [Step(["reset", "--hard", b.head],
                  f"Move this branch back to where it was at {b.label}.")]
    if b.wip:
        steps.append(Step(["stash", "apply", b.wip], "Bring back the unsaved changes from that moment."))
    return Plan("Restore a backup", steps)


def connect_remote(url: str) -> Plan:
    return Plan("Connect to GitHub", [Step(
        ["remote", "add", "origin", url],
        "Remember this GitHub URL under the name 'origin'.")])


def init_repo() -> Plan:
    return Plan("Start tracking this folder", [Step(
        ["init"], "Turn this folder into a git project.")])


# --- conflict / detached-HEAD helpers (also used by rescue) -------------------

def finish_merge() -> Plan:
    return Plan("Finish combining", [
        Step(["add", "--all"], "Mark the conflicts as resolved (you've edited the files)."),
        Step(["commit", "--no-edit"], "Save the combined result."),
    ], summary="Only do this after removing the <<<<<<< / >>>>>>> markers from each file.")


def abort_merge(s: RepoState) -> Plan:
    if s.rebasing:
        return Plan("Cancel rebase", [Step(["rebase", "--abort"], "Go back to how things were before the rebase.")])
    if s.reverting:
        return Plan("Cancel undo", [Step(["revert", "--abort"], "Go back to how things were before the undo.")])
    return Plan("Cancel combining", [Step(["merge", "--abort"], "Go back to how things were before the pull/merge.")])


def branch_here() -> Plan:
    name = time.strftime("rescued-%m%d-%H%M")
    return Plan("Make a branch here", [Step(
        ["switch", "--create", name],
        f"Give this spot a branch name ('{name}') so commits made here aren't lost.")])


def back_to_branch() -> Plan:
    return Plan("Go back to my branch", [Step(["switch", "-"], "Return to the branch you were on before.")])
