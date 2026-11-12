"""The branch map: a simplified picture of the repo for people new to git.

Not a full commit graph. Each branch gets one row ("lane") showing only its
own recent commits, starting from the commit it grew out of. The main branch
(the "trunk") is the top row. Commits that exist only on GitHub are drawn
hollow, and unsaved changes hang off "you are here". A Ghost is the suggested
next step, drawn faintly on top (like an editor's autocomplete).

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
from rich.text import Text

from . import git
from .git import RepoState

PER_LANE = 6  # commits shown per branch
TRUNK_COMMITS = 8
MAX_BRANCHES = 6
LANE_COLORS = ["#7dd3fc", "#c4b5fd", "#fcd34d", "#86efac", "#f9a8d4", "#fdba74"]
GHOST = "italic #8b93a7"  # the suggested next step: faint, like autocomplete text
SELECTED = "b black on #f5b85c"  # the save under the cursor
OK, WARN = "#7bd88f", "#f5c26b"
SUBJECT_MAX = 44


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
    # trunk | branch | remote (GitHub's copy of a branch that went its own way) |
    # github (a branch only on GitHub; upstream is its remote ref) | detached
    kind: str
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


@dataclass
class Ghost:
    """What the suggested next step would add to the map. Nothing here exists yet."""
    saves: int = 0  # new saves where you are (or on `branch`)
    branch: str | None = None  # a new branch starting where you are
    merge: tuple[str, str] | None = None  # (source, target): source combined into target
    tails: dict[str, str] = field(default_factory=dict)  # lane name -> short marker, e.g. "⇡ upload"


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

    # Branches someone else made, only on GitHub so far. Ones already combined
    # into the trunk (here or on GitHub) have nothing new to show, so they're left out.
    github = []
    if s.remotes:
        t = by_name.get(trunk)
        github = git.github_branches(root, [x for x in (trunk if t else None, t and t["upstream"]) if x])
    room = max(0, MAX_BRANCHES - len(shown))
    hidden += max(0, len(github) - room)
    github = github[:room]
    for name, ref in github:
        sha = git.run(["rev-parse", ref], root).stdout.strip()
        lane = Lane(name, "github", upstream=ref, tip=sha)
        if sha in claimed:
            lane.fork = sha
            lanes.append(lane)
        else:
            add(lane, [ref] + ([f"^{trunk}"] if trunk in by_name else []), PER_LANE, remote_only=True)

    # Show: trunk, then the current branch, then the rest, then branches only on
    # GitHub; GitHub rows under their branch.
    rank = {b["name"]: i for i, b in enumerate(shown)}
    rank.update({name: len(shown) + i for i, (name, _) in enumerate(github)})

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
        if lane.kind == "github" and lane.name == branch:  # getting it makes it a local branch
            lane.kind, lane.behind, lane.ahead = "branch", 0, 0
            for c in lane.commits:
                c.remote_only = False
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
                                   badge=f"[{OK}]new branch[/]"))
    out.head = at
    return out


def after_save(g: Graph, message: str) -> Graph:
    """The graph once your unsaved changes become a new commit where you are."""
    out = copy.deepcopy(g)
    lane = next((l for l in out.lanes if l.current), None)
    if lane is None or out.head is None:
        return out
    n = sum(c.sha.startswith("(new save") for c in lane.commits)
    new = Commit(f"(new save {n + 1})" if n else "(new save)", [out.head], int(time.time()), message)
    if not lane.commits:
        lane.fork = lane.fork or out.head
    lane.commits.append(new)
    lane.tip = out.head = new.sha
    lane.badge = f"[{OK}]saved your changes[/]"
    return out


def after_merge(g: Graph, source: str, target: str) -> Graph:
    """The graph once `source` is combined into `target` (always a merge commit) and you're on target."""
    out = after_switch(g, target)
    into = next((l for l in out.lanes if l.is_branch and l.name == target), None)
    src = next((l for l in out.lanes if l.is_branch and l.name == source), None)
    if into is None or src is None or into.tip is None:
        return out
    new = Commit("(merge)", [into.tip, src.tip or ""], int(time.time()), f"Combine {source} into {target}")
    if not into.commits:
        into.fork = into.fork or into.tip
    into.commits.append(new)
    into.tip = out.head = new.sha
    src.badge = f"[dim]combined into {escape(target)}[/]"
    into.badge = f"[{OK}]new combined save[/]"
    return out


def after_revert(g: Graph, subject: str) -> Graph:
    """The graph once a save that reverses `subject` is added where you are."""
    out = after_save(g, f"Undo “{subject}”")
    lane = next((l for l in out.lanes if l.current), None)
    if lane is not None:
        lane.badge = f"[{OK}]new save that undoes it[/]"
    return out


def after_uncommit(g: Graph) -> Graph:
    """The graph once your latest save is un-saved (its changes become unsaved again)."""
    out = copy.deepcopy(g)
    lane = next((l for l in out.lanes if l.current), None)
    if lane is None or not lane.commits or lane.commits[-1].sha != out.head:
        return out
    gone = lane.commits.pop()
    out.head = lane.tip = gone.parents[0] if gone.parents else None
    lane.badge = f"[{OK}]latest save removed[/]"
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


_SEP = " [dim]·[/] "


def _short(text: str, n: int = SUBJECT_MAX) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _note(lane: Lane, g: Graph, s: RepoState, unsaved: int, compact: bool,
          tail: str | None = None, room: int | None = None) -> str:
    """What's written after a row. tail: the ghost's marker for this row.

    room: characters available. Parts that don't fit are dropped from the
    end, and the latest save's description is shortened to what's left.
    """
    parts = []
    if _here(lane, g):
        here = "[b reverse] you are here [/]"
        if unsaved:
            here += f" [{WARN}]✎ {unsaved} unsaved file{'s' if unsaved != 1 else ''}[/]"
        parts.append(here)
    if compact:
        return _SEP.join(parts + ([lane.badge] if lane.badge else []))
    if lane.kind == "remote":
        parts.append("[dim]GitHub has saves you don't, and you have saves it doesn't[/]")
    elif lane.kind == "github":
        parts.append(f"[{WARN}]⇣ only on GitHub, not on this computer yet[/]")
    elif lane.kind == "detached":
        parts.append(f"[{WARN}]looking at an old save, not on a branch[/]")
    elif lane.merged:
        parts.append(f"[dim]already combined into {escape(g.trunk or 'main')}[/]")
    elif not lane.commits:
        parts.append("[dim]no saves of its own yet[/]")
    if lane.is_branch and s.remotes:
        if not lane.upstream:
            parts.append("[dim]not on GitHub yet[/]")
        elif lane.ahead and lane.behind:
            parts.append(f"[{WARN}]↑{lane.ahead} not uploaded[/]")
        elif lane.behind:
            parts.append(f"[{WARN}]↓{lane.behind} on GitHub only[/]")
        elif lane.ahead:
            parts.append(f"[{WARN}]↑{lane.ahead} not uploaded[/]")
        else:
            parts.append(f"[{OK}]✓[/] [dim]on GitHub[/]")
    if tail:
        parts.append(f"[{GHOST}]{escape(tail)}[/]")
    out, used = [], 0
    for part in parts:
        n = Text.from_markup(part).cell_len + (3 if out else 0)
        if room is not None and out and used + n > room:
            return _SEP.join(out)
        out.append(part)
        used += n
    # What the row is about: its latest save. Without this every row is just dots.
    mine = lane.commits if lane.kind == "github" else [c for c in lane.commits if not c.remote_only]
    if mine and lane.kind != "remote":
        age = ago(mine[-1].time)
        fits = SUBJECT_MAX if room is None else min(SUBJECT_MAX, room - used - (3 if out else 0) - len(age) - 3)
        if fits >= 10:
            out.append(f"[dim]“{escape(_short(mine[-1].subject, fits))}” {age}[/]")
    return _SEP.join(out)


def label_width(g: Graph) -> int:
    """Characters taken by the branch names at the start of each row."""
    return min(max((len(l.name) for l in g.lanes), default=0) + 2, 20)


def render(g: Graph, s: RepoState, selected: tuple[int, int] | None = None,
           compact: bool = False, unsaved: int | None = None, held: str | None = None,
           ghost: Ghost | None = None, max_width: int | None = None) -> str:
    """The map as Rich markup, one line per lane.

    selected: (lane index, column) to highlight. compact: only mark where you
    are (for previews). unsaved: override the unsaved-file count shown.
    held: branch picked up to combine into another (its label is highlighted).
    ghost: the suggested next step, drawn faintly (dashed lines, ◌ for saves
    that don't exist yet). max_width: shorten the notes after each row to fit.
    """
    if not g.lanes:
        return "[dim]No saves yet. Use “Save my work” to make your first one.[/]"
    if unsaved is None:
        unsaved = len({c.path for c in s.changes}) + len(s.untracked)
    col = columns(g)
    ncols = max(col.values(), default=0) + 1
    ghost = ghost or Ghost()
    by_name = {lane.name: r for r, lane in enumerate(g.lanes) if lane.is_branch}
    merge = None  # (source row, target row), when the ghost combines two shown branches
    if ghost.merge and ghost.merge[0] in by_name and ghost.merge[1] in by_name:
        merge = (by_name[ghost.merge[0]], by_name[ghost.merge[1]])
        ncols += 1  # the combined save gets a column of its own, right of everything
    ncols += ghost.saves  # room for suggested saves after "you are here"
    row_of = {c.sha: i for i, lane in enumerate(g.lanes) for c in lane.commits}
    glyph = [[" "] * ncols for _ in g.lanes]
    link = [["  "] * ncols for _ in g.lanes]
    color = [[LANE_COLORS[0]] * ncols for _ in g.lanes]
    faint: set[tuple[int, int]] = set()  # cells that belong to the ghost
    ends: dict[int, int] = {}  # lane row -> its last drawn column
    through: dict[int, list[tuple[int, str]]] = {}  # row -> fork lines passing below it: (column, color)

    for r, lane in enumerate(g.lanes):
        lane_color = lane_color_for(lane)
        color[r] = [lane_color] * ncols
        cols = [col[c.sha] for c in lane.commits]
        fork_col = col.get(lane.fork) if lane.fork else None
        start = fork_col if fork_col is not None else (cols[0] if cols else None)
        if start is None:
            continue
        end = cols[-1] if cols else start
        ends[r] = end
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
            for rr in range(lo, hi):
                through.setdefault(rr, []).append((fork_col, lane_color))
        elif cols and lane.truncated and start > 0:
            glyph[r][start - 1] = "┄"
            link[r][start - 1] = "┄┄"

    here_row = next((r for r, lane in enumerate(g.lanes) if _here(lane, g)), None)
    # A combining save pulls in another row's latest save: draw that row's line
    # over to it, so you can see where the work went.
    for r, lane in enumerate(g.lanes):
        for c in lane.commits:
            src = row_of.get(c.parents[1]) if c.is_merge else None
            if src is None or src == r or src not in ends or g.lanes[src].commits[-1].sha != c.parents[1]:
                continue  # not on the map, or that branch went on after being combined
            x = col[c.sha]
            if ends[src] >= x or (src == here_row and ghost.saves):
                continue
            for xx in range(ends[src], x):
                link[src][xx] = "──"
                if xx > ends[src]:
                    glyph[src][xx] = "┼" if glyph[src][xx] == "│" else "─"
            glyph[src][x] = "╯" if r < src else "╮"
            lo, hi = sorted((src, r))
            for rr in range(lo + 1, hi):
                glyph[rr][x] = {" ": "│", "─": "┼"}.get(glyph[rr][x], glyph[rr][x])
                color[rr][x] = color[src][x]

    if merge and merge[0] in ends and merge[1] in ends:
        src, dst = merge
        last = ncols - 1
        for r in (src, dst):
            for x in range(ends[r], last):
                link[r][x] = "╌╌"
                faint.add((r, x))
                if x > ends[r]:
                    glyph[r][x] = "╌"
        glyph[dst][last] = "◌"
        glyph[src][last] = "╯" if dst < src else "╮"
        lo, hi = sorted((src, dst))
        for rr in range(lo + 1, hi):
            if glyph[rr][last] == " ":
                glyph[rr][last] = "┆"
        faint.update((rr, last) for rr in range(lo, hi + 1))
    else:
        merge = None
    if ghost.saves and not ghost.branch and here_row in ends:
        for x in range(ends[here_row], ends[here_row] + ghost.saves):
            link[here_row][x] = "╌╌"
            glyph[here_row][x + 1] = "◌"
            faint.update({(here_row, x), (here_row, x + 1)})

    width = label_width(g)
    if ghost.branch:
        width = max(width, min(len(ghost.branch) + 2, 20))
    lines = []
    for r, lane in enumerate(g.lanes):
        name = lane.name if len(lane.name) <= width - 2 else lane.name[: width - 3] + "…"
        label = ("▶ " if lane.current else "⇣ " if lane.kind == "github" else "  ") + name
        bold = lane.current or (selected is not None and selected[0] == r)
        if held is not None and lane.is_branch and lane.name == held:
            label = f"[{SELECTED}]{escape(label):<{width}}[/]"
        elif selected is not None and selected[0] == r:
            # The row under the cursor: a block in the lane's own color, so moving
            # up and down is easy to follow (bold alone looks like "you are here").
            label = f"[b #0b0e14 on {lane_color_for(lane)}]{escape(label):<{width - 1}}[/] "
        else:
            label = f"[b]{escape(label):<{width}}[/]" if bold else f"{escape(label):<{width}}"
        cells = []
        remote = {c.sha for c in lane.commits if c.remote_only}
        for x in range(ncols):
            ch, ln, colr = glyph[r][x], link[r][x], color[r][x]
            sha = next((c.sha for c in lane.commits if col[c.sha] == x), None)
            # A branch only on GitHub keeps its color: its ○ dots and ⇣ mark say where it is.
            style = "dim" if (sha in remote and lane.kind != "github") or lane.kind == "remote" else colr
            if ch == "◉":
                style = "b #ffffff"
            link_style = "dim" if lane.kind == "remote" else colr
            if (r, x) in faint:
                link_style = GHOST
                if ch in "╌◌╯╮┆" or (ch == " " and ln.strip()):
                    style = GHOST
            if selected == (r, x):
                style = SELECTED
            if not ch.strip() and not ln.strip():
                cells.append(ch + ln)
            elif not ln.strip():
                cells.append(f"[{style}]{ch}[/]{ln}")
            else:
                cells.append(f"[{style}]{ch}[/][{link_style}]{ln}[/]")
        line = label + "".join(cells)  # every cell is 3 wide, so the notes line up
        here = _here(lane, g)
        room = max_width - (width + 3 * ncols + 1) if max_width else None
        note = _note(lane, g, s, unsaved, compact, room=room,
                     tail=ghost.tails.get(lane.name) if lane.is_branch else None)
        lines.append(line + " " + note if note else line.rstrip())
        if here and ghost.branch:
            lines.append(_ghost_branch(ghost, width, col.get(g.head or "", ends.get(r, 0)),
                                       ncols, through.get(r, [])))
    if g.hidden_branches and not compact:
        lines.append(f"[dim]  … and {g.hidden_branches} more branch{'es' if g.hidden_branches != 1 else ''}[/]")
    return "\n".join(lines)


def _ghost_branch(ghost: Ghost, width: int, at: int, ncols: int, through: list[tuple[int, str]]) -> str:
    """The row for a branch that doesn't exist yet, growing out of column `at`."""
    name = ghost.branch or ""
    name = name if len(name) <= width - 2 else name[: width - 3] + "…"
    lines_through = dict(through)
    cells = ""
    for x in range(at):
        cells += f"[{lines_through[x]}]│[/]  " if x in lines_through else "   "
    stem = "╰" + ("╌╌◌" * ghost.saves or "╌╌")
    pad = " " * max(0, 3 * (ncols - at) - len(stem))  # line the note up with the other rows
    return (f"[{GHOST}]{escape('  ' + name):<{width}}[/]{cells}"
            f"[{GHOST}]{stem}{pad} new branch, suggested[/]")


LEGEND_SHORT = "● save  ◆ combined  ○ only on GitHub  ◉ you  ◌ suggested"
LEGEND = ("[dim]● a save   ◆ two branches combined   ○ on GitHub only   "
          "◉ where you are   ◌ suggested next step   ┄ older saves   left = older, right = newer[/]")
