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
    return Plan("Cancel combining", [Step(["merge", "--abort"], "Go back to how things were before the pull/merge.")])


def branch_here() -> Plan:
    name = time.strftime("rescued-%m%d-%H%M")
    return Plan("Make a branch here", [Step(
        ["switch", "--create", name],
        f"Give this spot a branch name ('{name}') so commits made here aren't lost.")])


def back_to_branch() -> Plan:
    return Plan("Go back to my branch", [Step(["switch", "-"], "Return to the branch you were on before.")])
