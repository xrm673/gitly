"""Sort out conflicts: read the clashing spots, name the two sides, turn picks into a Plan.

When git can't combine two versions of the same lines, it writes both into the
file between markers:

    <<<<<<< HEAD
    the version on top
    ||||||| base          (only with merge.conflictStyle diff3 / zdiff3)
    the original
    =======
    the version below
    >>>>>>> login

gitly reads those spots from the file on disk, so spots the user already fixed
by hand are simply gone. Each spot is shown with the AI's explanation of both
versions and its recommendation (see ai.advise_conflict); the user picks, and
plan() turns the picks into a Plan to review. The new file contents come from
the two versions themselves. Only a "combined" pick uses text the AI wrote, and
the user sees it in full before choosing it.

"Yours" and "incoming" are not git's ours/theirs: in a rebase those are
swapped (the top version is the branch being replayed onto). sides() works out
which is which, so nothing else has to.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import git
from .git import RepoState
from .plan import Plan, Step

MINE, INCOMING, MINE_FIRST, INCOMING_FIRST, COMBINED, SKIP = (
    "mine", "incoming", "both_mine_first", "both_incoming_first", "combined", "skip")
PICKS = (MINE, INCOMING, MINE_FIRST, INCOMING_FIRST, COMBINED)  # what the AI may recommend

_OPEN = re.compile(r"<{7}(?: |$)")
_BASE = re.compile(r"\|{7}(?: |$)")
_MID = re.compile(r"={7}$")
_CLOSE = re.compile(r">{7}(?: |$)")
_ANY_MARKER = re.compile(r"(?m)^(?:<{7}|>{7})(?: |$)")
CONTEXT_LINES = 4


@dataclass
class Advice:
    """The AI's take on one spot. pick is None when it made no (usable) recommendation."""
    mine_does: str = ""
    incoming_does: str = ""
    pick: str | None = None
    combined: str = ""
    reason: str = ""
    unsure: bool = False


@dataclass
class Spot:
    path: str
    n: int  # 1-based, within the file
    mine: str | None  # None: that side deleted the file
    incoming: str | None
    base: str | None = None  # the original, when git wrote it (diff3)
    line: int = 0  # first line of the spot in the file; 0 for a whole-file spot
    before: str = ""  # a few lines around the spot, for the AI
    after: str = ""
    raw: str = ""  # the spot exactly as in the file, markers and all (kept on "skip")
    # The clash is about the whole file: deleted on one side, binary, or markers
    # gitly couldn't read. Then the only choices are one side's file or the other's.
    whole_file: bool = False
    binary: bool = False
    unreadable: bool = False  # text file, but its markers didn't parse
    mine_bytes: bytes | None = None  # whole-file spots: each side's file as git stored it
    incoming_bytes: bytes | None = None
    advice: Advice | None = None

    def options(self) -> list[str]:
        """What the user can pick here, recommended first."""
        if self.whole_file:
            opts = [MINE, INCOMING]
        else:
            opts = [MINE, INCOMING, MINE_FIRST, INCOMING_FIRST]
            if self.advice and self.advice.pick == COMBINED:
                opts.append(COMBINED)
        rec = self.advice.pick if self.advice else None
        if rec in opts:
            opts.remove(rec)
            opts.insert(0, rec)
        return opts + [SKIP]

    def text_for(self, pick: str) -> str:
        """The spot's new text for a pick (text spots only)."""
        mine, inc = self.mine or "", self.incoming or ""
        if pick == MINE:
            return mine
        if pick == INCOMING:
            return inc
        if pick == MINE_FIRST:
            return _stack(mine, inc, self.before)
        if pick == INCOMING_FIRST:
            return _stack(inc, mine, self.before)
        if pick == COMBINED and self.advice:
            text = self.advice.combined
            if text and not text.endswith("\n") and (mine.endswith("\n") or inc.endswith("\n")):
                text += "\n"
            return text
        return self.raw


@dataclass
class ConflictFile:
    path: str
    kind: str  # git's two letters, see RepoState.conflict_kinds
    spots: list[Spot]
    pieces: list[str | Spot] = field(default_factory=list)  # text files: the file, in order
    original: bytes | None = None  # the file on disk when gitly read it (None: not there)
    remove: bool = False  # deleted on both sides: nothing to choose, it just goes

    @property
    def done_by_hand(self) -> bool:
        """The user already removed every marker; it only needs marking as sorted out."""
        return not self.spots and not self.remove


@dataclass
class Sides:
    mine: str  # short names for the UI, e.g. "main" or "your save “add login”"
    incoming: str
    op: str  # merge | pull | rebase | revert
    flipped: bool = False  # the top version in the file (git's "ours") is the incoming one
    mine_ref: str = "HEAD"
    incoming_ref: str = "MERGE_HEAD"


# ------------------------------------------------------------------ reading

def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _subject(root: str, ref: str) -> str:
    r = git.run(["log", "-1", "--format=%s", ref, "--"], root)
    return r.stdout.strip() if r.ok else ""


def _ref_name(root: str, ref: str) -> str:
    """A branch name for a commit, e.g. "login" or "origin/main" ("" if none fits)."""
    r = git.run(["name-rev", "--name-only", "--no-undefined",
                 "--refs=refs/heads/*", "--refs=refs/remotes/*", ref], root)
    name = r.stdout.strip() if r.ok else ""
    return name.removeprefix("remotes/")


def sides(s: RepoState) -> Sides:
    gitdir = Path(git.run(["rev-parse", "--absolute-git-dir"], s.root).stdout.strip())
    if s.rebasing:
        d = gitdir / "rebase-merge" if (gitdir / "rebase-merge").exists() else gitdir / "rebase-apply"
        onto = _read(d / "onto")
        subject = _subject(s.root, "REBASE_HEAD")
        mine = f"your save “{subject}”" if subject else "your save"
        return Sides(mine, (_ref_name(s.root, onto) if onto else "") or "the branch you're moving onto",
                     "rebase", flipped=True, mine_ref="REBASE_HEAD", incoming_ref="HEAD")
    if s.reverting:
        subject = _subject(s.root, "REVERT_HEAD")
        return Sides(s.branch or "your version", f"undoing “{subject}”" if subject else "the undo",
                     "revert", incoming_ref="REVERT_HEAD")
    name = _ref_name(s.root, "MERGE_HEAD")
    remote = any(name.startswith(r + "/") for r in s.remotes)
    incoming = "GitHub's version" if remote else name or "the other branch"
    return Sides(s.branch or "your version", incoming, "pull" if remote else "merge")


def history(root: str, sd: Sides, path: str, n: int = 4) -> tuple[list[str], list[str]]:
    """Save descriptions on each side that touched `path` since the two split: (yours, incoming)."""
    def log(*args: str) -> list[str]:
        r = git.run(["log", "--format=%s", *args], root)
        return [line for line in r.stdout.splitlines() if line] if r.ok else []

    if sd.op == "revert":
        return log(f"-{n}", "HEAD", "--", path), [f"Undo of: {x}" for x in log("-1", "REVERT_HEAD", "--")]
    base = git.run(["merge-base", sd.mine_ref, sd.incoming_ref], root).stdout.strip()

    def side(ref: str) -> list[str]:
        return log(f"-{n}", f"{base}..{ref}" if base else ref, "--", path)

    mine = log("-1", "REBASE_HEAD", "--") if sd.op == "rebase" else side(sd.mine_ref)
    return mine, side(sd.incoming_ref)


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def _stack(a: str, b: str, before: str = "") -> str:
    """One version after the other.

    Both sides often add a block after the same blank lines, which git keeps
    outside the spot. Stacked, the blocks would then touch, so the blank lines
    just above the spot are repeated between them (none for a list of imports).
    """
    if a and not a.endswith("\n"):
        a += "\n"
    if a.strip() and b.strip() and a[-2:] != "\n\n" and not b.startswith(("\n", "\r\n")):
        lines = before.splitlines(keepends=True)
        gap = []
        while lines and not lines[-1].strip():
            gap.insert(0, lines.pop())
        if lines:  # only when there's code above the blank lines, not a file that starts blank
            a += "".join(gap)
    return a + b


def parse(path: str, text: str, flipped: bool = False) -> list[str | Spot] | None:
    """The file as plain text pieces and Spots, or None if its markers don't make sense."""
    pieces: list[str | Spot] = []
    plain: list[str] = []
    top: list[str] = []
    base: list[str] = []
    bottom: list[str] = []
    raw: list[str] = []
    where, start, has_base, n = "out", 0, False, 0
    for i, line in enumerate(text.splitlines(keepends=True), 1):
        bare = line.rstrip("\r\n")
        if where == "out":
            if _OPEN.match(bare):
                where, start, has_base = "top", i, False
                top, base, bottom, raw = [], [], [], [line]
                if plain:
                    pieces.append("".join(plain))
                    plain = []
            elif _CLOSE.match(bare):
                return None
            else:
                plain.append(line)
            continue
        raw.append(line)
        if _OPEN.match(bare):
            return None  # nested
        if where == "top" and _BASE.match(bare):
            where, has_base = "base", True
        elif where in ("top", "base") and _MID.match(bare):
            where = "bottom"
        elif where == "bottom" and _CLOSE.match(bare):
            n += 1
            a, b = "".join(top), "".join(bottom)
            mine, inc = (b, a) if flipped else (a, b)
            pieces.append(Spot(path, n, mine, inc, base="".join(base) if has_base else None,
                               line=start, raw="".join(raw)))
            where = "out"
        elif where == "bottom" and _MID.match(bare):
            return None
        else:
            {"top": top, "base": base, "bottom": bottom}[where].append(line)
    if where != "out":
        return None  # never closed
    if plain:
        pieces.append("".join(plain))
    for i, p in enumerate(pieces):
        if isinstance(p, Spot):
            if i > 0 and isinstance(pieces[i - 1], str):
                p.before = "".join(pieces[i - 1].splitlines(keepends=True)[-CONTEXT_LINES:])
            if i + 1 < len(pieces) and isinstance(pieces[i + 1], str):
                p.after = "".join(pieces[i + 1].splitlines(keepends=True)[:CONTEXT_LINES])
    return pieces


def read_file(root: str, path: str, kind: str, sd: Sides) -> ConflictFile:
    try:
        data: bytes | None = (Path(root) / path).read_bytes()
    except OSError:
        data = None
    text = None
    if data is not None and not _is_binary(data):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    unreadable = False
    if kind in ("UU", "AA") and text is not None:
        pieces = parse(path, text, sd.flipped)
        if pieces is not None:
            return ConflictFile(path, kind, [p for p in pieces if isinstance(p, Spot)], pieces, data)
        unreadable = True

    stage_mine, stage_inc = (3, 2) if sd.flipped else (2, 3)
    mine_b = git.blob(root, f":{stage_mine}:{path}")
    inc_b = git.blob(root, f":{stage_inc}:{path}")
    if mine_b is None and inc_b is None:
        return ConflictFile(path, kind, [], original=data, remove=True)
    binary = any(b is not None and _is_binary(b) for b in (mine_b, inc_b))

    def shown(b: bytes | None) -> str | None:
        if b is None:
            return None
        return "" if binary else b.decode("utf-8", errors="replace")

    spot = Spot(path, 1, shown(mine_b), shown(inc_b), whole_file=True, binary=binary,
                unreadable=unreadable, mine_bytes=mine_b, incoming_bytes=inc_b)
    return ConflictFile(path, kind, [spot], original=data)


def load(s: RepoState) -> tuple[list[ConflictFile], Sides]:
    sd = sides(s)
    return [read_file(s.root, p, s.conflict_kinds.get(p, "UU"), sd) for p in s.conflicts], sd


# ------------------------------------------------------------------ AI advice

def clean_combined(text: str) -> str | None:
    """The AI's combined text, or None if it isn't usable as-is."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].lstrip().startswith("```") and lines[-1].strip().startswith("```"):
        text = "".join(lines[1:-1])  # it wrapped the code in a fence anyway
    if not text.strip() or _ANY_MARKER.search(text) or re.search(r"(?m)^={7}$", text):
        return None
    return text


# ------------------------------------------------------------------ the plan

def _count(picks: list[str], sd: Sides) -> str:
    words = {
        MINE: f"yours ({sd.mine})", INCOMING: f"incoming ({sd.incoming})",
        MINE_FIRST: "both, yours first", INCOMING_FIRST: "both, incoming first",
        COMBINED: "the AI's combined version", SKIP: "left for you to edit",
    }
    order = [MINE, INCOMING, MINE_FIRST, INCOMING_FIRST, COMBINED, SKIP]
    parts = []
    for p in order:
        k = picks.count(p)
        if k:
            parts.append(words[p] if len(picks) == 1 else f"{words[p]} ×{k}")
    return ", ".join(parts)


def plan(files: list[ConflictFile], sd: Sides, choices: dict[tuple[str, int], str],
         op_name: str = "combining") -> Plan:
    """Turn the user's picks into a Plan. choices: (path, spot n) -> pick; missing means skip."""
    steps: list[Step] = []
    add: list[str] = []
    rm: list[str] = []
    notes: list[str] = []
    for f in files:
        if f.remove:
            rm.append(f.path)
            notes.append(f"{f.path}: deleted on both sides, so it goes")
            continue
        if f.done_by_hand:
            add.append(f.path)
            notes.append(f"{f.path}: you already sorted this one out")
            continue
        picks = [choices.get((f.path, sp.n), SKIP) for sp in f.spots]
        notes.append(f"{f.path}: {_count(picks, sd)}")
        if all(p == SKIP for p in picks):
            continue
        if f.spots[0].whole_file:
            sp, pick = f.spots[0], picks[0]
            data = sp.mine_bytes if pick == MINE else sp.incoming_bytes
            side = "yours" if pick == MINE else "incoming"
            if data is None:
                rm.append(f.path)
            else:
                steps.append(Step([], f"Put the {side} version of the file in place.",
                                  write=data, path=f.path, expect=f.original))
                add.append(f.path)
            continue
        text = "".join(p if isinstance(p, str) else p.text_for(picks[p.n - 1]) for p in f.pieces)
        skipped = picks.count(SKIP)
        why = f"Write the version you picked: {_count(picks, sd)}."
        if skipped:
            why += f" The {'spot' if skipped == 1 else 'spots'} you skipped keep their markers."
        steps.append(Step([], why, write=text.encode("utf-8"), path=f.path, expect=f.original))
        if not skipped:
            add.append(f.path)
    if rm:
        steps.append(Step(["rm", "--quiet", "--", *rm], "Remove the file, as you picked."
                          if len(rm) == 1 else "Remove these files, as you picked."))
    if add:
        steps.append(Step(["add", "--", *add], "Mark them as sorted out."))
    return Plan("Sort out the conflicts", steps, note="\n".join(notes),
                summary=f"Nothing is final yet: until you finish, cancelling the {op_name} "
                        "still puts everything back the way it was.")


def changed_since_read(root: str, p: Plan) -> list[str]:
    """Files a plan would write that changed after gitly read them."""
    out = []
    for st in p.steps:
        if st.write is None:
            continue
        try:
            now: bytes | None = (Path(root) / st.path).read_bytes()
        except OSError:
            now = None
        if now != st.expect:
            out.append(st.path)
    return out


def write(root: str, st: Step) -> git.Result:
    start = time.monotonic()
    args = [f"(write {st.path})"]
    target = Path(root) / st.path
    try:
        if not target.resolve().is_relative_to(Path(root).resolve()):
            return git.Result(args, 1, "", f"{st.path} is outside this project", time.monotonic() - start)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(st.write or b"")
    except OSError as e:
        return git.Result(args, 1, "", f"Couldn't write {st.path}: {e}", time.monotonic() - start)
    return git.Result(args, 0, f"Wrote {st.path}", "", time.monotonic() - start)
