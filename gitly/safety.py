"""Risk levels and hard limits on what gitly will run.

- risk(): safe / caution / danger, shown to the user and used to decide how
  much confirmation is needed.
- blocked_reason(): commands gitly refuses to run at all (mostly things that
  could execute arbitrary code, hang the TUI, or destroy recovery data).
  Applies to every plan, but matters most for AI-suggested commands.
"""

from __future__ import annotations

SAFE, CAUTION, DANGER = "safe", "caution", "danger"
RISK_ORDER = [SAFE, CAUTION, DANGER]

ALLOWED_SUBCOMMANDS = {
    "add", "branch", "checkout", "cherry-pick", "clean", "commit", "diff", "fetch",
    "init", "log", "merge", "mv", "pull", "push", "rebase", "remote", "reset",
    "restore", "revert", "rm", "show", "stash", "status", "switch", "tag",
    "update-ref",
}

# Flags that run external programs, open interactive editors, or change where
# git executes. Any of these could hang the TUI or run arbitrary code.
BLOCKED_FLAGS = {
    "-i", "--interactive", "--exec", "--upload-pack", "--receive-pack",
    "--exec-path", "--mirror", "-e", "--edit",
}
# Interactive "patch" mode only matters for commands that change files.
PATCH_MODE_COMMANDS = {"add", "checkout", "restore", "reset", "stash", "commit"}


def _has(args: list[str], *flags: str) -> bool:
    for a in args:
        if a in flags:
            return True
        for f in flags:
            if f.startswith("--") and a.startswith(f + "="):
                return True
    return False




def blocked_reason(args: list[str]) -> str | None:
    if not args:
        return "empty command"
    sub = args[0]
    if sub.startswith("-"):
        return "global git options (like -c or -C) are not allowed"
    if sub == "config":
        # Only your name/email. Other config keys (core.pager, core.sshCommand,
        # aliases, hooks...) can make git run arbitrary programs.
        rest = [a for a in args[1:] if a not in ("--global", "--local")]
        if len(rest) == 2 and rest[0] in ("user.name", "user.email"):
            return None
        return "gitly only sets user.name and user.email"
    if sub not in ALLOWED_SUBCOMMANDS:
        return f"gitly doesn't run `git {sub}`"
    for a in args[1:]:
        if a in BLOCKED_FLAGS or any(a.startswith(f + "=") for f in BLOCKED_FLAGS if f.startswith("--")):
            return f"`{a}` needs an interactive editor or runs other programs"
        if "\n" in a or "\0" in a:
            return "arguments can't contain newlines"
    if sub in PATCH_MODE_COMMANDS and _has(args[1:], "-p", "--patch"):
        return "interactive patch mode isn't supported in gitly"
    if sub == "rebase" and _has(args[1:], "-x"):
        return "`rebase -x` runs other programs"
    if sub == "update-ref" and not any(a.startswith("refs/gitly/") for a in args[1:]):
        return "update-ref is only used for gitly backups"
    if sub == "remote" and len(args) > 1 and args[1] not in {"-v", "add", "set-url", "show"}:
        return f"`git remote {args[1]}` isn't supported"
    return None


def risk(args: list[str]) -> str:
    if not args:
        return SAFE
    sub, rest = args[0], args[1:]

    if sub == "reset":
        return DANGER if _has(rest, "--hard", "--merge", "--keep") else CAUTION
    if sub == "push":
        if _has(rest, "--force", "-f", "--force-with-lease", "--delete", "-d", "--prune"):
            return DANGER
        if any(a.startswith("+") or a.startswith(":") for a in rest):
            return DANGER  # +refspec force-push / :branch delete
        return SAFE
    if sub == "clean":
        return DANGER
    if sub == "branch":
        if _has(rest, "-D", "--force", "-f"):
            return DANGER
        if _has(rest, "-d", "--delete", "-m", "-M", "--move"):
            return CAUTION
        return SAFE
    if sub == "checkout":
        # `checkout -- file` / `checkout .` throw away edits
        if "--" in rest or "." in rest or _has(rest, "-f", "--force"):
            return DANGER
        return SAFE
    if sub == "restore":
        staged_only = _has(rest, "--staged", "-S") and not _has(rest, "--worktree", "-W")
        return SAFE if staged_only else DANGER
    if sub == "stash":
        if rest[:1] in (["drop"], ["clear"]):
            return DANGER
        if rest[:1] == ["pop"] or rest[:1] == ["apply"]:
            return CAUTION
        return SAFE
    if sub == "switch":
        return DANGER if _has(rest, "--discard-changes", "-f", "--force") else SAFE
    if sub == "rm":
        return CAUTION
    if sub == "commit":
        return CAUTION if _has(rest, "--amend") else SAFE
    if sub in {"rebase", "merge", "revert", "cherry-pick", "pull"}:
        if sub == "merge" and _has(rest, "--abort"):
            return CAUTION
        return CAUTION
    if sub == "tag" and _has(rest, "-d", "--delete", "-f", "--force"):
        return CAUTION
    return SAFE
