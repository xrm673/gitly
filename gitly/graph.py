"""The branch map: a simplified picture of the repo for people new to git.

Not a full commit graph. Each branch gets one row ("lane") showing only its
own recent commits, starting from the commit it grew out of. The main branch
(the "trunk") is the top row. Commits that exist only on GitHub are drawn
hollow, and unsaved changes hang off "you are here".

build() reads the repo; render() turns the result into Rich markup;
positions() lists the cells a cursor can land on. after_*() return a copy of
the graph as it will look after an action, for the before/after preview.
"""

from __future__ import annotations

import copy
import heapq
import time
import zlib
from dataclasses import dataclass, field

from rich.markup import escape

from . import git
from .git import RepoState

PER_LANE = 6  # commits shown per branch
TRUNK_COMMITS = 8
MAX_BRANCHES = 6
LANE_COLORS = ["cyan", "magenta", "yellow", "green", "blue", "red"]


@dataclass
class Commit:
    sha: str
    parents: list[str]
    time: int
    subject: str
    remote_only: bool = False  # on GitHub, not on this computer yet

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass
class Lane:
    name: str
    kind: str  # trunk | branch | remote | detached
    commits: list[Commit] = field(default_factory=list)  # oldest first
    fork: str | None = None  # sha (on another lane) this lane grows out of
    truncated: bool = False  # it has older commits of its own that aren't shown
    current: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    merged: bool = False  # no commits of its own because they're already in the trunk
    tip: str | None = None  # the commit the branch points at
    badge: str = ""  # extra label in previews, e.g. "new branch"

    @property
    def is_branch(self) -> bool:
        return self.kind in ("trunk", "branch")


@dataclass
class Graph:
    trunk: str | None
    lanes: list[Lane]
    head: str | None
    hidden_branches: int = 0

    def commit(self, sha: str) -> Commit | None:
        for lane in self.lanes:
            for c in lane.commits:
                if c.sha == sha:
                    return c
        return None


# ----------------------------------------------------------------- reading git

def _log(root: str, revs: list[str], n: int) -> list[Commit]:
    """First-parent history for `revs`, newest first."""
    r = git.run(["log", "--first-parent", f"--max-count={n}",
                 "--format=%H%x1f%P%x1f%ct%x1f%s", *revs, "--"], root)
    out = []
    for line in r.stdout.splitlines() if r.ok else []:
        sha, parents, ct, subject = line.split("\x1f", 3)
        out.append(Commit(sha, parents.split(), int(ct), subject))
    return out


def _branch_refs(root: str) -> list[dict]:
    r = git.run(["for-each-ref", "refs/heads", "--sort=-committerdate", "--format="
                 "%(refname:short)%1f%(objectname)%1f%(upstream:short)%1f%(upstream:track,nobracket)"], root)
    refs = []
    for line in r.stdout.splitlines():
        name, sha, upstream, track = line.split("\x1f")
        ahead = behind = 0
        for part in track.split(","):
            word, _, num = part.strip().partition(" ")
            if word == "ahead":
                ahead = int(num)
            elif word == "behind":
                behind = int(num)
        refs.append({"name": name, "sha": sha, "upstream": upstream if track != "gone" else "",
                     "ahead": ahead, "behind": behind})
    return refs


def _trunk_name(root: str, names: list[str], current: str | None) -> str | None:
    for candidate in ("main", "master"):
        if candidate in names:
            return candidate
    r = git.run(["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], root)
    remote_default = r.stdout.strip().partition("/")[2]
    if remote_default in names:
        return remote_default
    return current or (names[0] if names else None)


def build(s: RepoState) -> Graph:
    if s.head_sha is None:
        return Graph(None, [], None)
    root = s.root
    refs = _branch_refs(root)
    trunk = _trunk_name(root, [b["name"] for b in refs], s.branch)
    claimed: dict[str, int] = {}  # sha -> lane index
    lanes: list[Lane] = []

    def add(lane: Lane, revs: list[str], n: int, remote_only: bool = False) -> Lane:
        raw = _log(root, revs, n + 1)
        own: list[Commit] = []
        for c in raw:
            if c.sha in claimed:
                lane.fork = c.sha
                break
            if len(own) == n:
                lane.truncated = True
                break
            c.remote_only = remote_only
            own.append(c)
        else:
            # Ran out of history unique to this lane: it grew out of the oldest one's parent.
            if own and own[-1].parents:
                lane.fork = own[-1].parents[0]
                lane.truncated = lane.fork not in claimed
        lane.commits = list(reversed(own))
        for c in lane.commits:
            claimed[c.sha] = len(lanes)
        lanes.append(lane)
        return lane

    def add_remote(lane: Lane, ref: dict) -> None:
        """Commits that are on GitHub but not here, for a lane's branch."""
        if not ref["behind"] or not ref["upstream"]:
            return
        idx = lanes.index(lane)
        if ref["ahead"] == 0:  # GitHub is simply further along: draw them after the tip
            for c in reversed(_log(root, [ref["upstream"], f"^{ref['name']}"], PER_LANE)):
                c.remote_only = True
                lane.commits.append(c)
                claimed[c.sha] = idx
        else:  # both sides have new commits: GitHub's version gets its own row
            add(Lane(f"{ref['name']} on GitHub", "remote"),
                [ref["upstream"], f"^{ref['name']}"], PER_LANE, remote_only=True)

    by_name = {b["name"]: b for b in refs}
    others = [b for b in refs if b["name"] != trunk]
    shown = ([b for b in others if b["name"] == s.branch] + [b for b in others if b["name"] != s.branch])
    hidden = max(0, len(shown) - MAX_BRANCHES)
    shown = shown[:MAX_BRANCHES]

    # A branch that another one grew out of should own the shared commits, so
    # claim lanes in order of how much each branch has of its own (fewest first).
    # The current branch goes last among ties so it shows as "starts here".
    def own_count(b: dict) -> int:
        if trunk not in by_name:
            return 0
        r = git.run(["rev-list", "--count", "--first-parent", b["name"], f"^{trunk}"], root)
        return int(r.stdout.strip() or 0)
    claim_order = sorted(shown, key=lambda b: (own_count(b), b["name"] == s.branch))

    if trunk in by_name:
        t = by_name[trunk]
        lane = add(Lane(trunk, "trunk", current=s.branch == trunk, upstream=t["upstream"] or None,
                        ahead=t["ahead"], behind=t["behind"], tip=t["sha"]), [trunk], TRUNK_COMMITS)
        lane.fork = None  # the trunk doesn't grow out of anything
        add_remote(lane, t)

    for b in claim_order:
        revs = [b["name"]] + ([f"^{trunk}"] if trunk in by_name else [])
        lane = Lane(b["name"], "branch", current=b["name"] == s.branch,
                    upstream=b["upstream"] or None, ahead=b["ahead"], behind=b["behind"], tip=b["sha"])
        if b["sha"] in claimed:
            lane.fork = b["sha"]  # points at a commit on another lane, nothing of its own
            lanes.append(lane)
        else:
            add(lane, revs, PER_LANE)
            if not lane.commits:
                lane.merged = True
                lane.fork = None
        add_remote(lane, b)

    if s.detached and s.head_sha not in claimed:
        revs = ["HEAD"] + ([f"^{trunk}"] if trunk in by_name else [])
        add(Lane("(no branch)", "detached", current=True, tip=s.head_sha), revs, PER_LANE)

    # Show: trunk, then the current branch, then the rest; GitHub rows under their branch.
    rank = {b["name"]: i for i, b in enumerate(shown)}

    def display_key(lane: Lane) -> tuple:
        if lane.kind == "trunk" or lane.name == f"{trunk} on GitHub":
            return (0, lane.kind == "remote")
        if lane.kind == "detached":
            return (1, False)
        base = lane.name.removesuffix(" on GitHub") if lane.kind == "remote" else lane.name
        return (2 + rank.get(base, len(rank)), lane.kind == "remote")

    lanes.sort(key=display_key)
    return Graph(trunk if trunk in by_name else None, lanes, s.head_sha, hidden)


# ----------------------------------------------------------------- layout

def columns(g: Graph) -> dict[str, int]:
    """Left-to-right position of every shown commit: parents always left of children."""
    shown = {c.sha: (c, i) for i, lane in enumerate(g.lanes) for c in lane.commits}
    after: dict[str, set[str]] = {sha: set() for sha in shown}  # sha -> commits that must come later
    for lane in g.lanes:
        chain = ([lane.fork] if lane.fork in shown else []) + [c.sha for c in lane.commits]
        for a, b in zip(chain, chain[1:]):
            after[a].add(b)
        for c in lane.commits:
            for p in c.parents:
                if p in shown:
                    after[p].add(c.sha)
    waiting = {sha: 0 for sha in shown}
    for sha, later in after.items():
        for b in later:
            waiting[b] += 1
    ready = [(shown[sha][0].time, shown[sha][1], sha) for sha, n in waiting.items() if n == 0]
    heapq.heapify(ready)
    col: dict[str, int] = {}
    while ready:
        _, _, sha = heapq.heappop(ready)
        col[sha] = len(col) + 1  # column 0 is kept for "older commits" markers
        for b in after[sha]:
            waiting[b] -= 1
            if waiting[b] == 0:
                heapq.heappush(ready, (shown[b][0].time, shown[b][1], b))
    return col


def positions(g: Graph) -> list[list[tuple[int, str]]]:
    """Per lane, the (column, sha) cells a cursor can land on, left to right.

    A branch with no commits of its own gets one cell: where it starts.
    """
    col = columns(g)
    out = []
    for lane in g.lanes:
        cells = [(col[c.sha], c.sha) for c in lane.commits]
        if not cells and lane.fork in col:
            cells = [(col[lane.fork], lane.fork)]
        out.append(cells)
    return out


def ago(ts: int, now: float | None = None) -> str:
    secs = max(0, int((now or time.time()) - ts))
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800),
                       ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


# ----------------------------------------------------------------- previews

def after_switch(g: Graph, branch: str) -> Graph:
    """The graph once you've moved onto `branch`."""
    out = copy.deepcopy(g)
    for lane in out.lanes:
        lane.current = lane.is_branch and lane.name == branch
        if lane.current:
            out.head = lane.tip
    return out


def after_new_branch(g: Graph, name: str, at: str) -> Graph:
    """The graph once a new branch `name` starts at commit `at` and you're on it."""
    out = copy.deepcopy(g)
    for lane in out.lanes:
        lane.current = False
    row = next((i for i, l in enumerate(out.lanes) if any(c.sha == at for c in l.commits)), 0)
    out.lanes.insert(row + 1, Lane(name, "branch", fork=at, current=True, tip=at,
                                   badge="[green]new branch[/]"))
    out.head = at
    return out


def after_save(g: Graph, message: str) -> Graph:
    """The graph once your unsaved changes become a new commit where you are."""
    out = copy.deepcopy(g)
    lane = next((l for l in out.lanes if l.current), None)
    if lane is None or out.head is None:
        return out
    new = Commit("(new save)", [out.head], int(time.time()), message)
    if not lane.commits:
        lane.fork = lane.fork or out.head
    lane.commits.append(new)
    lane.tip = out.head = new.sha
    lane.badge = "[green]saved your changes[/]"
    return out


# ----------------------------------------------------------------- drawing

def lane_color_for(lane: Lane) -> str:
    """Same branch, same color, even when rows are added or reordered."""
    if lane.kind == "trunk":
        return LANE_COLORS[0]
    name = lane.name.removesuffix(" on GitHub")
    return LANE_COLORS[1 + zlib.crc32(name.encode()) % (len(LANE_COLORS) - 1)]


def _glyph(c: Commit, head: str | None) -> str:
    if c.sha == head:
        return "◉"
    if c.remote_only:
        return "○"
    return "◆" if c.is_merge else "●"


def _here(lane: Lane, g: Graph) -> bool:
    if lane.kind == "remote":
        return False
    return lane.current or (lane.kind == "detached" and any(c.sha == g.head for c in lane.commits))


def _note(lane: Lane, g: Graph, s: RepoState, unsaved: int, compact: bool) -> str:
    parts = []
    if _here(lane, g):
        here = "[b reverse] you are here [/]"
        if unsaved:
            here += f" [yellow]✎ {unsaved} unsaved file{'s' if unsaved != 1 else ''}[/]"
        parts.append(here)
    if compact:
        return " [dim]·[/] ".join(parts + ([lane.badge] if lane.badge else []))
    if lane.kind == "remote":
        parts.append("[dim]GitHub has saves you don't, and you have saves it doesn't[/]")
    elif lane.kind == "detached":
        parts.append("[yellow]looking at an old save, not on a branch[/]")
    elif lane.merged:
        parts.append(f"[dim]already combined into {escape(g.trunk or 'main')}[/]")
    elif not lane.commits:
        parts.append("[dim]no saves of its own yet[/]")
    if lane.is_branch and s.remotes:
        if not lane.upstream:
            parts.append("[dim]not on GitHub yet[/]")
        elif lane.ahead and lane.behind:
            parts.append(f"[yellow]↑{lane.ahead} not uploaded[/]")
        elif lane.behind:
            parts.append(f"[yellow]↓{lane.behind} on GitHub only[/]")
        elif lane.ahead:
            parts.append(f"[yellow]↑{lane.ahead} not uploaded[/]")
        else:
            parts.append("[dim]in sync with GitHub[/]")
    return " [dim]·[/] ".join(parts)


def render(g: Graph, s: RepoState, selected: tuple[int, int] | None = None,
           compact: bool = False, unsaved: int | None = None) -> str:
    """The map as Rich markup, one line per lane.

    selected: (lane index, column) to highlight. compact: only mark where you
    are (for previews). unsaved: override the unsaved-file count shown.
    """
    if not g.lanes:
        return "[dim]No saves yet. Use “Save my work” to make your first one.[/]"
    if unsaved is None:
        unsaved = len({c.path for c in s.changes}) + len(s.untracked)
    col = columns(g)
    ncols = max(col.values(), default=0) + 1
    row_of = {c.sha: i for i, lane in enumerate(g.lanes) for c in lane.commits}
    glyph = [[" "] * ncols for _ in g.lanes]
    link = [["  "] * ncols for _ in g.lanes]
    color = [[LANE_COLORS[0]] * ncols for _ in g.lanes]

    for r, lane in enumerate(g.lanes):
        lane_color = lane_color_for(lane)
        color[r] = [lane_color] * ncols
        cols = [col[c.sha] for c in lane.commits]
        fork_col = col.get(lane.fork) if lane.fork else None
        start = fork_col if fork_col is not None else (cols[0] if cols else None)
        if start is None:
            continue
        end = cols[-1] if cols else start
        for x in range(start, end + 1):
            glyph[r][x] = "─"
        for x in range(start, end):
            link[r][x] = "──"
        for c, x in zip(lane.commits, cols):
            glyph[r][x] = _glyph(c, g.head)
        if fork_col is not None and lane.fork in row_of and row_of[lane.fork] != r:
            above = row_of[lane.fork] < r
            glyph[r][fork_col] = "╰" if above else "╭"
            lo, hi = sorted((row_of[lane.fork], r))
            for rr in range(lo + 1, hi):
                glyph[rr][fork_col] = {" ": "│", "─": "┼"}.get(glyph[rr][fork_col], glyph[rr][fork_col])
                color[rr][fork_col] = lane_color
        elif cols and lane.truncated and start > 0:
            glyph[r][start - 1] = "┄"
            link[r][start - 1] = "┄┄"

    width = min(max(len(l.name) for l in g.lanes) + 2, 20)
    lines = []
    for r, lane in enumerate(g.lanes):
        name = lane.name if len(lane.name) <= width - 2 else lane.name[: width - 3] + "…"
        label = ("▶ " if lane.current else "  ") + name
        bold = lane.current or (selected is not None and selected[0] == r)
        label = f"[b]{escape(label):<{width}}[/]" if bold else f"{escape(label):<{width}}"
        cells = []
        remote = {c.sha for c in lane.commits if c.remote_only}
        for x in range(ncols):
            ch, ln, colr = glyph[r][x], link[r][x], color[r][x]
            sha = next((c.sha for c in lane.commits if col[c.sha] == x), None)
            style = "dim" if sha in remote or lane.kind == "remote" else colr
            if ch == "◉":
                style = f"b reverse {colr}"
            if selected == (r, x):
                style = "b black on bright_yellow"
            cells.append(f"[{style}]{ch}[/][{'dim' if lane.kind == 'remote' else colr}]{ln}[/]"
                         if ch.strip() or ln.strip() else ch + ln)
        line = (label + "".join(cells)).rstrip()
        note = _note(lane, g, s, unsaved, compact)
        lines.append(line + ("  " + note if note else ""))
    if g.hidden_branches and not compact:
        lines.append(f"[dim]  … and {g.hidden_branches} more branch{'es' if g.hidden_branches != 1 else ''}[/]")
    return "\n".join(lines)


LEGEND = ("[dim]● a save   ◆ two branches combined   ○ on GitHub only   "
          "◉ where you are   ┄ older saves   left = older, right = newer[/]")
