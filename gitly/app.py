"""gitly's terminal UI (Textual)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Input, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option

from . import actions, ai, git, graph, precheck, rescue, safety, suggest
from .plan import Plan, Step
from .studylog import StudyLog

RISK_BADGE = {
    safety.SAFE: "[b $success]safe[/]",
    safety.CAUTION: "[b $warning]careful[/]",
    safety.DANGER: "[b $error]risky[/]",
}
STATUS_COLOR = {"new": "$success", "deleted": "$error", "renamed": "$secondary", "conflict": "#f9a8d4"}
AI_COLOR = "#b69cff"  # everything the AI wrote or picked is marked with a ✦ in this color
CMD = "b #e6e9f2 on #2a3042"  # a git command, shown as a little block

THEME = Theme(
    name="gitly", dark=True,
    primary="#8b9cff", secondary="#7dd3fc", accent="#f5b85c",
    success="#7bd88f", warning="#f5c26b", error="#ff7a85",
    foreground="#d7dbe7", background="#0e1016", surface="#12151d", panel="#1b1f2b",
)


# ----------------------------------------------------------------- modal screens

class ChooseScreen(ModalScreen):
    """Pick one option from a list. Returns the option id or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, title: str, options: list[tuple[str, str]], hint: str = ""):
        super().__init__()
        self.title_text, self.options, self.hint = title, options, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/]", classes="dialog-title")
            if self.hint:
                yield Static(self.hint, classes="muted")
            yield OptionList(*[Option(label, id=key) for key, label in self.options])
            yield Static("[dim]↑↓ move · Enter choose · Esc back[/]")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class AskTextScreen(ModalScreen):
    """Ask for one line of text. Returns the text or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", hint: str = "", value: str = "",
                 password: bool = False):
        super().__init__()
        self.title_text, self.placeholder, self.hint, self.value = title, placeholder, hint, value
        self.password = password

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/]", classes="dialog-title")
            if self.hint:
                yield Static(self.hint, classes="muted")
            yield Input(value=self.value, placeholder=self.placeholder, password=self.password)
            yield Static("[dim]Enter to continue · Esc to cancel[/]")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class PlanScreen(ModalScreen):
    """Show exactly what will run. Returns True to run, False to cancel."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, plan: Plan):
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        p = self.plan
        with Vertical(classes="dialog wide"):
            src = {"ai": f"  [{AI_COLOR}]✦[/] [dim]suggested by AI, check it[/]", "rescue": ""}.get(p.source, "")
            yield Static(f"[b]{escape(p.title)}[/]{src}", classes="dialog-title")
            with VerticalScroll(classes="plan-body"):
                if p.note:
                    yield Static(escape(p.note) if p.source == "ai" else p.note, classes="note")
                if p.summary:
                    yield Static(escape(p.summary), classes="muted")
                if p.preview:
                    now, after = p.preview
                    yield Static(f"[b]Now[/]\n{now}\n\n[b]After[/]\n{after}", classes="preview")
                if p.steps:
                    yield Static("[b]This will run:[/]")
                for i, st in enumerate(p.steps, 1):
                    cmd = escape(st.display)
                    badge = RISK_BADGE[st.risk]
                    blocked = f"\n   [$error]✗ blocked: {escape(st.blocked)}[/]" if st.blocked else ""
                    yield Static(f"[dim]{i}.[/] [{CMD}] {cmd} [/]  {badge}\n   {escape(st.why)}{blocked}",
                                 classes="step")
            if p.blocked:
                yield Static("[$error]gitly won't run this plan because a step is blocked.[/]")
                yield Button("Close", id="cancel", variant="default")
            elif not p.steps:
                yield Button("OK", id="cancel", variant="primary")
            else:
                if p.risk == safety.DANGER:
                    yield Static("[$error]This can throw away work.[/] gitly will save a backup first "
                                 "(Undo → Restore a backup).\nType [b]yes[/] and press Enter to run.")
                    yield Input(placeholder="yes", id="confirm")
                with Horizontal(classes="buttons"):
                    yield Button("Run it", id="run", variant="error" if p.risk == safety.DANGER else "success",
                                 disabled=p.risk == safety.DANGER)
                    yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        for sel in ("#confirm", "#run", "#cancel"):
            w = self.query(sel)
            if w and not w.first().disabled:
                w.first().focus()
                break

    @on(Input.Changed, "#confirm")
    def typed(self, event: Input.Changed) -> None:
        self.query_one("#run", Button).disabled = event.value.strip().lower() != "yes"

    @on(Input.Submitted, "#confirm")
    def confirm_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip().lower() == "yes":
            self.dismiss(True)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "run")


class ProblemScreen(ModalScreen):
    """Explain a failure and offer fixes. Returns a fix index, 'ask', 'input', or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, failed: git.Result, diagnosis: rescue.Rescue | None, can_ask: bool):
        super().__init__()
        self.failed, self.diagnosis, self.can_ask = failed, diagnosis, can_ask

    def compose(self) -> ComposeResult:
        d = self.diagnosis
        with Vertical(classes="dialog wide"):
            title = d.title if d else "That didn't work"
            yield Static(f"[b $error]✗[/] [b]{escape(title)}[/]", classes="dialog-title")
            with VerticalScroll(classes="plan-body"):
                if d:
                    yield Static(escape(d.explanation), classes="note")
                yield Static(f"[dim]$ {escape(git.display(self.failed.args))}\n"
                             f"{escape(self.failed.output[-1500:])}[/]", classes="raw")
            opts: list[Option] = []
            if d:
                opts += [Option(f"Fix: {escape(f.title)}", id=f"fix:{i}") for i, f in enumerate(d.fixes)]
                if d.needs_input == "remote_url":
                    opts.append(Option("Connect to GitHub (paste URL)…", id="input"))
                if d.needs_input == "identity":
                    opts.append(Option("Set my name and email…", id="input"))
            if self.can_ask:
                opts.append(Option("Ask AI what to do", id="ask"))
            opts.append(Option("Close", id="close"))
            yield OptionList(*opts)

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        self.dismiss(None if oid == "close" else oid)


class PrecheckScreen(ModalScreen):
    """Warn about risky files before a save. Returns 'leave_out', 'anyway', or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, findings: list[precheck.Finding]):
        super().__init__()
        self.findings = findings

    def compose(self) -> ComposeResult:
        n = len(self.findings)
        with Vertical(classes="dialog wide"):
            yield Static(f"[b $warning]⚠[/] [b]Check {'this' if n == 1 else 'these'} before saving[/]",
                         classes="dialog-title")
            with VerticalScroll(classes="plan-body"):
                for f in self.findings:
                    extra = (f"\n   [dim]Will be added to .gitignore as {escape(f.gitignore)}[/]"
                             if f.gitignore else "")
                    yield Static(f"[b]{escape(f.headline)}[/]  {escape(f.path)}\n   {escape(f.detail)}{extra}",
                                 classes="step")
            yield OptionList(
                Option("Leave these out and save the rest  [dim](recommended)[/]", id="leave_out"),
                Option("Save everything anyway", id="anyway"),
                Option("Cancel, I'll fix it myself", id="cancel"),
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        self.dismiss(None if oid == "cancel" else oid)


class TextScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,enter", "dismiss(None)", "Close")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self.title_text, self.body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Static(f"[b]{escape(self.title_text)}[/]", classes="dialog-title")
            with VerticalScroll(classes="plan-body"):
                yield Static(self.body)
            yield Static("[dim]Esc to close[/]")


class ThinkingScreen(ModalScreen):
    def __init__(self, doing: str = "asking the AI for a plan"):
        super().__init__()
        self.doing = doing

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[b]Thinking…[/]  {escape(self.doing)}", classes="dialog-title")
            yield LoadingIndicator()


class BranchMap(Vertical, can_focus=True):
    """The branch map panel on the home screen. Tab focuses it.

    While focused, arrow keys pick a save. n: new branch here · Enter: switch ·
    Space then Enter: combine one branch into another · u: undo a save ·
    Esc: back to the menu.
    """

    BINDINGS = [
        Binding("escape", "back", "Back to menu"),
        Binding("left", "move(0, -1)", "Move", show=False),
        Binding("right", "move(0, 1)", "Move", show=False),
        Binding("up", "move(-1, 0)", "Move", show=False),
        Binding("down", "move(1, 0)", "Move", show=False),
        Binding("n", "new_branch", "New branch from here"),
        Binding("enter", "enter", "Switch / drop"),
        Binding("space", "pick_up", "Combine…"),
        Binding("u", "undo", "Undo this save"),
    ]
    DEFAULT_CSS = """
    BranchMap { border: round $panel-lighten-2; padding: 0 1; min-height: 7; }
    BranchMap:focus { border: round $accent; }
    BranchMap #map-wrap { height: 1fr; scrollbar-size-horizontal: 1;
                          scrollbar-background: $background; scrollbar-color: $panel-lighten-2; }
    BranchMap #map { width: auto; height: auto; }
    BranchMap #map-detail { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._sig: str | None = None  # what the graph was built from; rebuild only when it changes
        self.s: git.RepoState | None = None
        self.g: graph.Graph | None = None
        self.cells: list[list[tuple[int, str]]] = []
        self.sel: tuple[int, int] | None = None  # (lane index, index into its cells)
        self.held: str | None = None  # branch picked up with Space, to combine into another
        self.ghost: graph.Ghost | None = None  # the suggested next step, drawn faintly

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="map-wrap"):
            yield Static(id="map")
        yield Static(id="map-detail")

    def on_mount(self) -> None:
        self.border_title = "Branches"
        self.query_one("#map-wrap").can_focus = False  # arrow keys move the selection, not the scroll
        self._titles()

    def _titles(self, focused: bool = False) -> None:
        self.border_subtitle = graph.LEGEND_SHORT

    def set_ghost(self, ghost: graph.Ghost | None) -> None:
        if ghost != self.ghost:
            self.ghost = ghost
            self._draw()

    def on_focus(self) -> None:
        self._titles(focused=True)
        self._draw(focused=True)

    def on_resize(self) -> None:
        self._draw()  # the notes after each row are cut to the width

    def on_blur(self) -> None:
        self.held = None
        self._titles(focused=False)
        self._draw(focused=False)

    # ---- state

    def update_state(self, s: git.RepoState | None) -> None:
        """Called whenever the home screen refreshes. Rebuilds only when branches moved."""
        self.s = s
        if s is None or s.head_sha is None:
            self.g, self.cells, self.sel, self._sig = None, [], None, None
            self.query_one("#map-detail", Static).update("")
            self.query_one("#map", Static).update(
                "[dim]Your branches will show up here after your first save.[/]" if s
                else "[dim]Start tracking this folder to see branches here.[/]")
            return
        sig = f"{s.head_sha} {s.branch}\n{git.refs_signature(s.root)}"
        if sig != self._sig or self.g is None:
            keep = self.selected()
            self.g = graph.build(s)
            self.cells = graph.positions(self.g)
            self._sig = sig
            self.sel = ((keep and self._find(keep[1], lane=keep[0].name)) or self._find(s.head_sha)
                        or next(((r, 0) for r, c in enumerate(self.cells) if c), None))
        self._draw()

    def _find(self, sha: str | None, lane: str | None = None) -> tuple[int, int] | None:
        if lane is not None:
            for r, cells in enumerate(self.cells):
                for i, (_, cell_sha) in enumerate(cells):
                    if cell_sha == sha and self.g.lanes[r].name == lane:
                        return r, i
            return None
        for r, cells in enumerate(self.cells):
            for i, (_, cell_sha) in enumerate(cells):
                if cell_sha == sha and (self.g.lanes[r].commits or self.g.lanes[r].current):
                    return r, i
        for r, cells in enumerate(self.cells):  # fall back to any row showing it
            for i, (_, cell_sha) in enumerate(cells):
                if cell_sha == sha:
                    return r, i
        return None

    def selected(self) -> tuple[graph.Lane, str] | None:
        if self.sel is None or self.g is None:
            return None
        r, i = self.sel
        return self.g.lanes[r], self.cells[r][i][1]

    def _draw(self, focused: bool | None = None) -> None:
        if self.g is None or self.s is None:
            return
        focused = self.has_focus if focused is None else focused
        cell = (self.sel[0], self.cells[self.sel[0]][self.sel[1]][0]) if self.sel and focused else None
        self.query_one("#map", Static).update(graph.render(
            self.g, self.s, selected=cell, held=self.held, ghost=self.ghost,
            max_width=self.query_one("#map-wrap").size.width or None))
        self.query_one("#map-detail", Static).update(
            self._detail() if focused else
            "[b]Tab[/] [dim]pick a save, then branch from it, switch, combine or undo[/]")

    def _detail(self) -> str:
        sel = self.selected()
        if sel is None:
            return "[dim]Nothing to select yet.[/]"
        lane, sha = sel
        if self.held:
            target = (f"Enter brings it into [b]{escape(lane.name)}[/]"
                      if lane.is_branch and lane.name != self.held else "move to another branch's row")
            return (f"[{graph.SELECTED}] Carrying {escape(self.held)} [/]  ↑ ↓ {target} · "
                    "esc puts it down")
        c = self.g.commit(sha)
        if lane.commits:
            where = "only on GitHub" if c and c.remote_only else f"on {escape(lane.name)}"
            head = (f"[b]“{escape(c.subject)}”[/]  [dim]{sha[:7]} · {graph.ago(c.time)} · {where}[/]"
                    if c else f"[dim]{sha[:7]}[/]")
        else:
            head = (f"[b]{escape(lane.name)}[/] starts at “{escape(c.subject) if c else sha[:7]}” "
                    "[dim]and has no saves of its own yet[/]")
        return head  # the keys are listed in the footer

    def action_move(self, dr: int, dc: int) -> None:
        if self.sel is None:
            return
        r, i = self.sel
        if dc:
            i = max(0, min(len(self.cells[r]) - 1, i + dc))
        else:
            x = self.cells[r][i][0]
            rr = r + dr
            while 0 <= rr < len(self.cells) and not self.cells[rr]:
                rr += dr
            if not 0 <= rr < len(self.cells):
                return
            r, i = rr, min(range(len(self.cells[rr])), key=lambda j: abs(self.cells[rr][j][0] - x))
        self.sel = (r, i)
        self._draw()
        # Keep the selected cell visible: rows start with a label, then 3 characters per column.
        label_w = graph.label_width(self.g)
        cell_x = label_w + 3 * self.cells[r][i][0]
        wrap = self.query_one("#map-wrap")
        if cell_x + 3 > wrap.scroll_x + wrap.size.width:
            wrap.scroll_to(x=cell_x + 3 - wrap.size.width, animate=False)
        elif cell_x < wrap.scroll_x + label_w:
            wrap.scroll_to(x=max(0, cell_x - label_w), animate=False)

    # ---- actions

    def _busy_reason(self) -> str | None:
        s = self.s
        if s is None or self.selected() is None:
            return "Nothing to act on yet."
        if s.conflicts or s.merging or s.rebasing or s.reverting:
            return "Finish or cancel the merge in progress first (main screen)."
        return None

    async def _make_room(self, blocking: list[str], reason: str) -> tuple[bool, Plan | None]:
        """Unsaved files in the way of an action. Offers to save them first.

        reason completes "You changed <files>, and ...". Returns (go on?, save plan).
        """
        s = self.s
        if not blocking:
            return True, None
        shown = ", ".join(escape(b) for b in blocking[:5]) + (" …" if len(blocking) > 5 else "")
        choice = await self.app.push_screen_wait(ChooseScreen(
            "Your unsaved changes are in the way", [
                ("save", "Save my changes first, then go  [dim](recommended)[/]"),
                ("cancel", "Cancel"),
            ], hint=(f"You changed [b]{shown}[/], and {escape(reason)}. "
                     "Git won't go ahead without overwriting your edits. Saving them first keeps "
                     "them safe on the branch you're on now.")))
        self.app.log_.event("map_in_the_way", files=len(blocking), choice=choice or "cancel")
        if choice != "save":
            return False, None
        save = await self.app.prepare_save(s)
        return save is not None, save

    def _unsaved(self) -> int:
        return len({c.path for c in self.s.changes}) + len(self.s.untracked)

    @work(exclusive=True, group="map")
    async def action_new_branch(self) -> None:
        if reason := self._busy_reason():
            self.notify(reason, severity="warning")
            return
        await self.new_branch(self.selected()[1])

    async def new_branch(self, sha: str, name: str | None = None) -> None:
        """Start a branch at `sha`. Asks for the name unless one is given (an accepted suggestion)."""
        s, g = self.s, self.g
        here = sha == s.head_sha
        c = g.commit(sha)
        label = "where you are" if here else f"the save “{c.subject if c else sha[:7]}”"
        name = name or await self.app.ask_branch_name(s, label)
        if not name:
            return
        blocking = [] if here else await asyncio.to_thread(git.unsaved_in_the_way, s, sha)
        ok, save = await self._make_room(blocking, f"{label} has a different version")
        if not ok:
            return
        before = graph.after_save(g, save.steps[-1].args[-1]) if save else g
        after = graph.after_new_branch(before, name, sha)
        carrying = 0 if save else self._unsaved()
        plan = actions.from_map(
            "New branch", actions.branch_steps(name, None if here else sha[:12], label, carrying),
            summary=f"Start '{name}' from {label}.",
            preview=(graph.render(g, s, compact=True),
                     graph.render(after, s, compact=True, unsaved=carrying)),
            save_first=save)
        self.app.log_.event("map_action", action="new_branch", here=here, save_first=save is not None)
        await self.app.execute(plan)  # refreshes the home screen, map included

    def action_back(self) -> None:
        if self.held:
            self.held = None
            self._draw()
        else:
            self.screen.query_one("#menu").focus()

    def action_enter(self) -> None:
        if self.held:
            self.combine()
        else:
            self.switch()

    def action_pick_up(self) -> None:
        if reason := self._busy_reason():
            self.notify(reason, severity="warning")
            return
        lane, _ = self.selected()
        if self.held == lane.name:
            self.held = None
        elif lane.kind == "remote":
            self.notify("This row is GitHub's copy. Use “Get latest” on the main screen to bring it in.")
            return
        elif not lane.is_branch:
            self.notify("Only branches can be combined. Press n to make a branch here first.")
            return
        else:
            self.held = lane.name
        self._draw()

    def _unstage_first(self, save: Plan | None) -> list:
        """Git won't merge or revert while some files are marked for saving and others aren't."""
        if save is None and self.s.staged and self.s.head_sha:
            return [Step(["restore", "--staged", "."],
                                 "Unmark files that were half-marked for saving. Your edits stay exactly "
                                 "as they are; git just won't do this while some files are marked.")]
        return []

    @work(exclusive=True, group="map")
    async def combine(self) -> None:
        source, (lane, _) = self.held, self.selected()
        if not lane.is_branch or lane.name == source:
            self.notify("Move to the row of the branch that should receive it, then press Enter.")
            return
        self.held = None
        self._draw()
        if reason := self._busy_reason():
            self.notify(reason, severity="warning")
            return
        await self.combine_branches(source, lane.name)

    async def combine_branches(self, source: str, target: str) -> None:
        """Bring `source` into `target`, with a conflict forecast and a before/after picture."""
        s, g = self.s, self.g
        if await asyncio.to_thread(git.is_ancestor, s.root, source, target):
            self.notify(f"Everything in {source} is already in {target}. Nothing to combine.")
            return
        unsaved = git.unsaved_paths(s)
        incoming = await asyncio.to_thread(git.files_changed, s.root, f"{target}...{source}")
        moving = (await asyncio.to_thread(git.files_changed, s.root, "HEAD", target)
                  if s.branch != target else set())
        blocking = sorted(unsaved & (incoming | moving))
        them = "it" if len(blocking) == 1 else "them"
        ok, save = await self._make_room(blocking, f"the work coming from '{source}' changes {them} too")
        if not ok:
            return
        status, files = await asyncio.to_thread(git.predict_merge, s.root, target, source)
        if status == "clean":
            note = "[$success]✓ No conflicts expected.[/] gitly checked this without touching your files."
        elif status == "conflict":
            note = (f"[$warning]⚠ Both branches changed {escape(', '.join(files[:5]))} in different ways.[/] "
                    "Git will stop and mark the spots so you can pick what to keep, then choose "
                    "“I fixed the conflicts, finish”. You can cancel the combining at any point.")
        else:
            note = "[dim]gitly couldn't check for conflicts ahead of time (needs git 2.38 or newer).[/]"
        before = graph.after_save(g, save.steps[-1].args[-1]) if save else g
        after = graph.after_merge(before, source, target)
        carrying = 0 if save else self._unsaved()
        plan = actions.from_map(
            "Combine branches", self._unstage_first(save) + actions.merge_steps(source, target, s.branch),
            summary=f"Bring the work from '{source}' into '{target}'.", note=note,
            preview=(graph.render(g, s, compact=True),
                     graph.render(after, s, compact=True, unsaved=carrying)),
            save_first=save)
        self.app.log_.event("map_action", action="merge", prediction=status, conflicts=len(files),
                            save_first=save is not None)
        await self.app.execute(plan)  # refreshes the home screen, map included

    @work(exclusive=True, group="map")
    async def action_undo(self) -> None:
        if reason := self._busy_reason():
            self.notify(reason, severity="warning")
            return
        lane, sha = self.selected()
        s, g = self.s, self.g
        c = g.commit(sha)
        if c is None or c.remote_only:
            self.notify("That save is only on GitHub. Use “Get latest” first.")
            return
        if not await asyncio.to_thread(git.is_ancestor, s.root, sha, "HEAD"):
            self.notify(f"That save isn't part of the branch you're on. Switch to {lane.name} "
                        "first (Enter), then undo it.")
            return
        is_head = sha == s.head_sha
        pushed = bool(s.upstream) and await asyncio.to_thread(git.is_ancestor, s.root, sha, s.upstream)
        method = "revert"
        if is_head and not pushed and not c.is_merge and c.parents:
            method = await self.app.push_screen_wait(ChooseScreen(f"Undo “{c.subject}”", [
                ("uncommit", "Un-save it: its changes go back to being unsaved  [dim](recommended)[/]"),
                ("revert", "Keep it, and add a new save that reverses it"),
            ], hint="This save isn't on GitHub yet, so it can simply be un-saved. Nothing is lost either way."))
            if method is None:
                return
        unsaved = git.unsaved_paths(s)
        if method == "uncommit":
            save, notes = None, []
            steps = actions.uncommit_steps()
            after = graph.after_uncommit(g)
            unsaved_after = len(unsaved | await asyncio.to_thread(git.files_in_commit, s.root, sha))
        else:
            touched = await asyncio.to_thread(git.files_in_commit, s.root, sha)
            blocking = sorted(unsaved & touched)
            them = "it" if len(blocking) == 1 else "them"
            ok, save = await self._make_room(blocking, f"undoing “{c.subject}” would change {them} too")
            if not ok:
                return
            steps = self._unstage_first(save) + actions.revert_steps(sha, c.subject, c.is_merge)
            notes = []
            if pushed:
                notes.append("This save is already on GitHub, so gitly adds an opposite save instead of "
                             "erasing it. Erasing shared saves causes trouble for anyone who already has them.")
            if c.is_merge:
                notes.append("[$warning]Heads up:[/] after undoing a merge, merging the same branch again "
                             "won't bring those changes back by itself, because git thinks they're already "
                             "in. If you change your mind later, undo this undo instead.")
            if not is_head:
                notes.append("Later saves stay as they are; only this save's changes are reversed. If later "
                             "saves built on it, git may ask you to sort out conflicts.")
            before = graph.after_save(g, save.steps[-1].args[-1]) if save else g
            after = graph.after_revert(before, c.subject)
            unsaved_after = 0 if save else len(unsaved)
        plan = actions.from_map(
            "Undo a save", steps, summary=f"Undo “{c.subject}”.", note="\n\n".join(notes),
            preview=(graph.render(g, s, compact=True),
                     graph.render(after, s, compact=True, unsaved=unsaved_after)),
            save_first=save)
        self.app.log_.event("map_action", action="undo", method=method, merge=c.is_merge,
                            pushed=pushed, head=is_head, save_first=save is not None)
        await self.app.execute(plan)  # refreshes the home screen, map included

    @work(exclusive=True, group="map")
    async def switch(self) -> None:
        if reason := self._busy_reason():
            self.notify(reason, severity="warning")
            return
        lane, _ = self.selected()
        if lane.kind == "remote":
            self.notify("This row is GitHub's copy. Use “Get latest” on the main screen to bring it in.")
            return
        if not lane.is_branch:
            self.notify("That's not a branch. Press n to start a branch there.")
            return
        if lane.current:
            self.notify(f"You're already on {lane.name}.")
            return
        s, g = self.s, self.g
        blocking = await asyncio.to_thread(git.unsaved_in_the_way, s, lane.name)
        ok, save = await self._make_room(blocking, f"'{lane.name}' has a different version")
        if not ok:
            return
        before = graph.after_save(g, save.steps[-1].args[-1]) if save else g
        after = graph.after_switch(before, lane.name)
        carrying = 0 if save else self._unsaved()
        plan = actions.from_map(
            "Switch branch", actions.switch_steps(lane.name, carrying),
            summary=f"Move to '{lane.name}'.",
            preview=(graph.render(g, s, compact=True),
                     graph.render(after, s, compact=True, unsaved=carrying)),
            save_first=save)
        self.app.log_.event("map_action", action="switch", carrying=carrying, save_first=save is not None)
        await self.app.execute(plan)  # refreshes the home screen, map included


# ----------------------------------------------------------------- main app

class Gitly(App):
    TITLE = "gitly"
    CSS = """
    Screen { background: $background; }
    * { border-title-color: $text-muted; border-subtitle-color: $text-muted; }
    #top { padding: 1 2 0 2; height: auto; }
    #title { width: 1fr; height: auto; }
    #aistatus { width: auto; height: auto; }
    #summary { padding: 0 2; height: auto; }
    #banner { margin: 1 2 0 2; padding: 0 1; height: auto; background: $warning 20%; display: none; }
    #banner.show { display: block; }
    #body { height: 1fr; padding: 1 2; }
    #left { width: 1fr; height: 100%; }
    #next { height: auto; border: round $primary; background: $primary 7%; padding: 0 1;
            border-title-color: $primary; border-title-style: bold; }
    #next.quiet { border: round $panel-lighten-2; background: transparent; border-title-color: $text-muted; }
    #next Static { height: auto; }
    #next-reason { padding-left: 2; color: $text-muted; }
    OptionList > .option-list--option-highlighted { background: $primary 14%; color: $text; text-style: none; }
    OptionList:focus > .option-list--option-highlighted { background: $primary 30%; color: $text;
                                                           text-style: bold; }
    OptionList > .option-list--separator { color: $panel-lighten-2; }
    #files { height: auto; max-height: 30%; border: round $panel-lighten-2; padding: 0 1; }
    #branchmap { height: 1fr; }
    #menu { width: 46; height: 100%; border: round $panel-lighten-2; background: transparent; padding: 0 1; }
    #menu:focus { border: round $accent; border-title-color: $accent; }
    ModalScreen { align: center middle; background: $background 70%; }
    .dialog { width: 64; max-width: 95%; height: auto; max-height: 90%;
              border: round $primary; background: $surface; padding: 1 2; }
    .dialog.wide { width: 90; }
    .dialog-title { margin-bottom: 1; }
    .dialog OptionList { border: none; background: transparent; padding: 0; }
    .dialog OptionList { height: auto; max-height: 16; margin: 1 0; }
    .dialog Input { margin: 1 0; }
    .plan-body { height: auto; max-height: 24; }
    .step { margin: 0 0 1 0; }
    .note { margin-bottom: 1; }
    .preview { margin-bottom: 1; padding: 0 1; border-left: thick $panel-lighten-2;
               text-wrap: nowrap; text-overflow: ellipsis; }
    .muted { color: $text-muted; margin-bottom: 1; }
    .raw { margin-top: 1; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    LoadingIndicator { height: 3; }
    """
    BINDINGS = [
        Binding("question_mark", "ask", "Ask in plain English"),
        Binding("x", "not_now", "Skip suggestion"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cwd: str, participant: str | None = None):
        super().__init__()
        self.cwd = cwd
        self.repo: git.RepoState | None = None
        self.log_ = StudyLog(participant)
        self.busy = False
        self.register_theme(THEME)
        self.theme = "gitly"
        # The next step (see suggest.py). The AI's take is cached per repo signature.
        self.suggestion: suggest.Suggestion | None = None
        self._shown: str | None = None  # key of the suggestion on screen, to log each one once
        self._dismissed: set[str] = set()
        self._sig: str | None = None
        self._sig_since = 0.0  # when the repo last changed under us
        self._ai_cache: dict[str, suggest.Suggestion] = {}
        self._ai_skip: set[str] = set()  # signatures the AI failed on
        self._ai_running = False
        self._ai_last = float("-inf")
        self._ai_failures = 0

    # ---- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="title")
            yield Static(id="aistatus")
        yield Static(id="summary")
        yield Static(id="banner")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                with Vertical(id="next"):
                    yield Static(id="next-title")
                    yield Static(id="next-reason")
                yield VerticalScroll(Static(id="filelist"), id="files")
                yield BranchMap(id="branchmap")
            yield OptionList(id="menu")
        yield Footer()

    def on_mount(self) -> None:
        self.log_.event("start", cwd=self.cwd, ai_available=ai.available(), model=ai.MODEL)
        files = self.home.query_one("#files")
        files.border_title = "Changes"
        files.can_focus = False  # Tab moves between the menu and the branch map
        self.home.query_one("#menu").border_title = "What do you want to do?"
        self.refresh_state()
        self.home.query_one("#menu").focus()
        self.set_interval(3, self._poll)

    def _poll(self) -> None:
        if not self.busy and self.screen is self.screen_stack[0]:
            self.refresh_state(keep_highlight=True, settle=True)

    @property
    def home(self):
        """The main screen (App.query_one would search whichever dialog is on top)."""
        return self.screen_stack[0]

    # ---- state → screen

    def refresh_state(self, keep_highlight: bool = False, settle: bool = False) -> None:
        """Re-read the repo and redraw the home screen.

        settle: the change may be the user (or their AI tool) mid-edit, so the
        AI waits until the files stop changing before it looks.
        """
        if not self.is_running or not self.screen_stack:
            return
        self.repo = git.state(self.cwd)
        s = self.repo
        top = self.home.query_one("#title", Static)
        summary = self.home.query_one("#summary", Static)
        banner = self.home.query_one("#banner", Static)
        files = self.home.query_one("#filelist", Static)

        if s is None:
            top.update(f"[b $background on $accent] gitly [/]  [dim]{escape(self._short_path(self.cwd))}[/]")
            summary.update("This folder isn't a git project yet.")
            banner.remove_class("show")
            files.update("[dim]Nothing tracked yet.[/]")
            self.home.query_one(BranchMap).update_state(None)
            self._set_menu(self._with_next([("init", "Start tracking this folder"), None,
                                            ("ask", "Ask in plain English  [dim]?[/]"), ("quit", "Quit")],
                                           None, settle), keep_highlight)
            return

        top.update(f"[b $background on $accent] gitly [/]  [dim]{escape(self._short_path(s.root))}[/]")
        summary.update(s.summary())

        warn = ""
        if s.conflicts:
            warn = (f"⚠ {len(s.conflicts)} file(s) have conflicts. Edit them to keep the right "
                    "version (remove the <<<<<<< >>>>>>> markers), then choose “I fixed the conflicts”.")
        elif s.merging or s.rebasing or s.reverting:
            warn = "⚠ A merge/rebase is in progress. Finish it or cancel it below."
        elif s.detached:
            warn = ("⚠ You're looking at an old snapshot, not a branch. New commits here can get lost: "
                    "make a branch here, or go back to your branch.")
        banner.update(warn)
        banner.set_class(bool(warn), "show")

        lines = []
        for c in s.conflicts:
            lines.append(f"[b #f9a8d4]conflict[/]  {escape(c)}")
        seen = set()
        for c in s.changes:
            if c.path in seen:
                continue
            seen.add(c.path)
            col = STATUS_COLOR.get(c.status, "$warning")
            lines.append(f"[{col}]{c.status:<9}[/] {escape(c.path)}")
        for u in s.untracked:
            lines.append(f"[$success]{'new':<9}[/] {escape(u)}")
        files.update("\n".join(lines) if lines else "[dim]No unsaved changes.[/]")

        menu: list = []
        if s.conflicts or s.merging or s.rebasing or s.reverting:
            menu += [("finish_merge", "[b]I fixed the conflicts, finish[/]"),
                     ("abort_merge", "Cancel combining"), None]
        if s.detached and not s.rebasing:  # a rebase always runs on a detached HEAD
            menu += [("branch_here", "[b]Make a branch here[/]"),
                     ("back_to_branch", "Go back to my branch"), None]
        n = len(seen) + len(s.untracked)
        save_hint = f"  [dim]({n} file{'s' if n != 1 else ''})[/]" if n else "  [dim](nothing to save)[/]"
        up_hint = f"  [dim]({s.ahead} new)[/]" if s.ahead else ""
        pull_hint = f"  [dim]({s.behind} waiting)[/]" if s.behind else ""
        menu += [
            ("save", "Save my work" + save_hint, not n),  # greyed out while there's nothing to save
            *([("explain", "What did I change?")] if s.dirty else []),
            ("upload", "Upload to GitHub" + up_hint),
            ("pull", "Get latest" + pull_hint),
            None,
            ("branches", "Branches…"),
            ("undo", "Undo…"),
            ("history", "History"),
        ]
        if not s.remotes:
            menu.append(("connect", "Connect to GitHub…"))
        menu += [None, ("ask", "Ask in plain English  [dim]?[/]"), ("quit", "Quit  [dim]q[/]")]
        self.home.query_one(BranchMap).update_state(s)  # before the suggestion: it reads the map
        self._set_menu(self._with_next(menu, s, settle), keep_highlight)

    @staticmethod
    def _short_path(path: str) -> str:
        home = str(Path.home())
        return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path

    # ---- the next step

    AI_SETTLE = 4.0  # seconds the files must sit still before the AI reads them
    AI_GAP = 20.0  # at least this long between two background AI calls

    def _with_next(self, menu: list, s: git.RepoState | None, settle: bool) -> list:
        """Work out the next step, show it, and put it at the top of the menu."""
        sg = self._suggest(s, settle)
        ai_made = sg.source == "ai"
        mark = f"[b {AI_COLOR}]✦[/]" if ai_made else "[b $primary]➜[/]"
        panel = self.home.query_one("#next")
        panel.query_one("#next-title", Static).update(f"{mark} [b]{escape(sg.title)}[/]")
        panel.query_one("#next-reason", Static).update(escape(sg.reason))
        panel.set_class(not sg.acceptable, "quiet")
        panel.border_title = "Next step"
        if self._ai_running:
            panel.border_subtitle = f"[{AI_COLOR}]✦[/] AI is reading your project…"
        elif not sg.acceptable:
            panel.border_subtitle = f"[{AI_COLOR}]✦[/] from the AI" if ai_made else ""
        else:
            panel.border_subtitle = ((f"[{AI_COLOR}]✦[/] suggested by AI · " if ai_made else "")
                                     + "Enter: do it · x: not now")
        self.home.query_one("#aistatus", Static).update(
            f"[{AI_COLOR}]✦[/] [dim]AI suggestions on[/]" if suggest.ambient() and self._ai_failures < 3
            else "[dim]✦ AI on request[/]" if ai.available()
            else "[dim]✦ AI off · press ? to turn it on[/]")
        self.home.query_one(BranchMap).set_ghost(suggest.ghost(sg, s) if sg.acceptable else None)
        first = next((i[0] for i in menu if i), None)
        if sg.acceptable and sg.menu != first:  # when it's already the top item, don't say it twice
            title = sg.title if len(sg.title) <= 38 else sg.title[:37].rstrip() + "…"
            menu = [("next", f"{mark} [b]{escape(title)}[/]\n  [dim]suggested next step[/]"), None] + menu
        return menu

    def _suggest(self, s: git.RepoState | None, settle: bool) -> suggest.Suggestion:
        g = self.home.query_one(BranchMap).g if s else None
        base = suggest.rules(s, g)
        sig = suggest.signature(s)
        now = time.monotonic()
        if sig != self._sig:
            self._sig, self._sig_since = sig, now if settle else 0.0
        sg = self._ai_cache.get(sig, base)
        if (sig not in self._ai_cache and sig not in self._ai_skip and not self._ai_running
                and self._ai_failures < 3 and self.app_focus and suggest.allowed(s, base)
                and suggest.ambient() and now - self._sig_since >= self.AI_SETTLE
                and now - self._ai_last >= self.AI_GAP):
            self._ai_running = True
            self._think(sig, s, g, base)
        if sg.key in self._dismissed:
            sg = suggest.Suggestion("none", "Nothing suggested right now",
                                    "You skipped the last suggestion. Pick anything from the menu.")
        if sg.key != self._shown:
            self._shown = sg.key
            self.log_.event("suggestion_shown", step=sg.kind, source=sg.source, title=sg.title)
        self.suggestion = sg
        return sg

    @work(group="ai")
    async def _think(self, sig: str, s: git.RepoState, g: graph.Graph | None,
                     base: suggest.Suggestion) -> None:
        """Let the AI refine the suggestion in the background. Failures stay quiet."""
        try:
            sg, tel = await asyncio.to_thread(suggest.from_ai, s, g, base)
        except Exception as e:  # never let a background guess take the app down
            self._ai_skip.add(sig)
            self._ai_failures += 1
            self.log_.event("ai_error", feature="ai_next", message=str(e))
        else:
            self._ai_failures = 0
            if len(self._ai_cache) > 32:
                self._ai_cache.clear()
            self._ai_cache[sig] = sg
            self.log_.event("ai_next", **tel)
        finally:
            self._ai_running = False
            self._ai_last = time.monotonic()
        if not self.busy and self.screen is self.screen_stack[0]:
            self.refresh_state(keep_highlight=True)

    def action_not_now(self) -> None:
        sg = self.suggestion
        if sg is None or not sg.acceptable or self.screen is not self.screen_stack[0]:
            return
        self._dismissed.add(sg.key)
        self.log_.event("suggestion_dismissed", step=sg.kind, source=sg.source)
        self.refresh_state()

    async def accept_suggestion(self) -> Plan | None:
        """The user took the suggested next step. Returns a plan, unless the step ran its own flow."""
        sg, s = self.suggestion, self.repo
        if sg is None or not sg.acceptable:
            return None
        self.log_.event("suggestion_accepted", step=sg.kind, source=sg.source)
        if sg.menu:
            return await self.plan_for(sg.menu)
        if s is None:
            return None
        bm = self.home.query_one(BranchMap)
        if sg.kind == "save":
            return await self.prepare_save(s, message=sg.message)
        if sg.kind == "split_save":
            return await self.split_save(s, sg)
        if sg.kind == "cleanup":
            return actions.delete_branch(sg.branch)
        if sg.kind == "new_branch" and s.head_sha:
            await bm.new_branch(s.head_sha, name=sg.branch)
        elif sg.kind == "merge":
            await bm.combine_branches(sg.branch, sg.target)
        return None

    async def split_save(self, s: git.RepoState, sg: suggest.Suggestion) -> Plan | None:
        findings = await asyncio.to_thread(precheck.check, s)
        if findings:  # something here shouldn't be saved: use the ordinary save, which deals with that
            return await self.prepare_save(s)
        g = self.home.query_one(BranchMap).g
        preview = None
        if g:
            after = g
            for grp in sg.groups:
                after = graph.after_save(after, grp.message)
            preview = (graph.render(g, s, compact=True), graph.render(after, s, compact=True, unsaved=0))
        return actions.split_save(s, [(grp.message, grp.files) for grp in sg.groups],
                                  note=sg.reason, preview=preview)

    def _set_menu(self, items: list, keep_highlight: bool) -> None:
        """items: (key, label) or (key, label, disabled); None draws a separator."""
        ol = self.home.query_one("#menu", OptionList)
        prev = ol.highlighted_option.id if (keep_highlight and ol.highlighted_option) else None
        ol.set_options([Option(i[1], id=i[0], disabled=len(i) > 2 and i[2]) if i else None for i in items])
        if prev:
            try:
                ol.highlighted = ol.get_option_index(prev)
                return
            except Exception:
                pass
        # Start on the suggested step, so Enter does the sensible thing.
        keys = [i[0] for i in items if i and not (len(i) > 2 and i[2])]
        want = self.suggestion.menu if self.suggestion and self.suggestion.acceptable else None
        start = "next" if "next" in keys else want if want in keys else keys[0] if keys else None
        if start:
            ol.highlighted = ol.get_option_index(start)

    # ---- menu dispatch

    @on(OptionList.OptionSelected, "#menu")
    def menu_selected(self, event: OptionList.OptionSelected) -> None:
        self.log_.event("menu", choice=event.option.id)
        self.handle(event.option.id)

    def action_ask(self) -> None:
        if self.screen is self.screen_stack[0]:
            self.handle("ask")

    def action_refresh(self) -> None:
        self.refresh_state(keep_highlight=True)

    @work(exclusive=True)
    async def handle(self, key: str) -> None:
        if key == "quit":
            self.exit()
            return
        if key == "ask":
            await self.ask_flow()
            return
        plan = await (self.accept_suggestion() if key == "next" else self.plan_for(key))
        if plan:
            await self.execute(plan)

    async def plan_for(self, key: str) -> Plan | None:
        """Run the flow behind a menu item. Returns the plan to review, if it ends in one."""
        s = self.repo
        if key == "init":
            return actions.init_repo()
        if s is None:
            return None
        if key == "save":
            if not s.dirty:
                self.notify("Nothing to save: no changes since your last save.")
                return None
            return await self.prepare_save(s)
        if key == "explain":
            await self.explain_flow(s)
        elif key == "upload":
            return actions.upload(s)
        elif key == "pull":
            return actions.get_latest(s)
        elif key == "branches":
            return await self.branches_flow(s)
        elif key == "undo":
            return await self.undo_flow(s)
        elif key == "history":
            lines = git.recent_log(s.root, 30)
            body = "\n".join(escape(l) for l in lines) or "[dim]No saves yet.[/]"
            await self.push_screen_wait(TextScreen("History (newest first)", body))
        elif key == "connect":
            return await self.connect_flow()
        elif key == "finish_merge":
            return actions.finish_merge(s)
        elif key == "abort_merge":
            return actions.abort_merge(s)
        elif key == "branch_here":
            return actions.branch_here()
        elif key == "back_to_branch":
            return actions.back_to_branch()
        return None

    async def branches_flow(self, s: git.RepoState) -> Plan | None:
        what = await self.push_screen_wait(ChooseScreen("Branches", [
            ("switch", "Switch to another branch"),
            ("new", "Start a new branch"),
            ("delete", "Delete a branch"),
        ], hint=f"You're on [b]{escape(s.branch or 'no branch')}[/]. A branch is a separate line of work."))
        others = [b for b in git.branches(s.root) if b != s.branch]
        if what == "new":
            name = await self.ask_branch_name(s, "where you are")
            if name:
                return actions.new_branch(name)
        elif what in ("switch", "delete"):
            if not others:
                self.notify("There are no other branches.")
                return None
            b = await self.push_screen_wait(ChooseScreen(
                "Switch to…" if what == "switch" else "Delete which branch?",
                [(b, escape(b)) for b in others]))
            if b:
                return actions.switch_branch(b) if what == "switch" else actions.delete_branch(b)
        return None

    async def undo_flow(self, s: git.RepoState) -> Plan | None:
        opts = []
        if s.head_sha:
            opts.append(("commit", "Undo my last save (keep the changes)"))
        if s.staged:
            opts.append(("unstage", "Unmark files for saving"))
        if s.changes:
            opts.append(("discard", "Throw away unsaved changes"))
        backups = git.backups(s.root)
        if backups:
            opts.append(("backup", f"Restore a gitly backup  [dim]({len(backups)})[/]"))
        if not opts:
            self.notify("Nothing to undo yet.")
            return None
        what = await self.push_screen_wait(ChooseScreen("Undo", opts))
        if what == "commit":
            return actions.undo_last_commit()
        if what == "unstage":
            return actions.unstage_all()
        if what == "discard":
            return actions.discard_changes()
        if what == "backup":
            bid = await self.push_screen_wait(ChooseScreen(
                "Restore which backup?", [(b.id, escape(b.label)) for b in backups],
                hint="gitly makes one automatically before anything risky."))
            b = next((b for b in backups if b.id == bid), None)
            if b:
                return actions.restore_backup(b)
        return None

    async def connect_flow(self) -> Plan | None:
        url = await self.push_screen_wait(AskTextScreen(
            "Connect to GitHub", placeholder="https://github.com/you/project.git",
            hint="Create an empty repository on github.com, then paste its URL here."))
        return actions.connect_remote(url) if url else None

    async def prepare_save(self, s: git.RepoState, message: str = "") -> Plan | None:
        """Pre-save check, AI description, and message prompt. Returns the save plan or None.

        message: a description the AI already wrote for these changes (the
        suggested next step), so it isn't asked again.
        """
        findings = await asyncio.to_thread(precheck.check, s)
        to_save = s
        if findings:
            choice = await self.push_screen_wait(PrecheckScreen(findings))
            self.log_.event("precheck", kinds=[f.kind for f in findings], choice=choice or "cancel")
            if choice is None:
                return None
            if choice == "leave_out":
                to_save = precheck.without(s, findings)
            else:
                findings = []
        ahead = self._ai_cache.get(suggest.signature(s))
        suggestion = message or (ahead.message if ahead and ahead.kind == "save" else "")
        if to_save is not s:
            suggestion = ""  # files were left out, so that description may not fit anymore
        if not suggestion and ai.available() and to_save.dirty:
            suggestion = await self.with_ai(
                ai.suggest_commit_message, to_save, doing="writing a description of your changes",
                event="ai_commit_message") or ""
        hint = "Describe what you changed in a few words. This label helps you find this save later."
        if suggestion:
            hint = "The AI suggested this from your changes. Edit it, or press Enter to keep it."
        msg = await self.push_screen_wait(AskTextScreen(
            "Save my work", placeholder="e.g. add login page", hint=hint, value=suggestion))
        if suggestion:
            self.log_.event("ai_commit_message_choice",
                            choice="cancelled" if not msg else "kept" if msg == suggestion else "edited")
        if not msg:
            return None
        return precheck.save_leaving_out(s, findings, msg) if findings else actions.save_work(s, msg)

    async def ask_branch_name(self, s: git.RepoState, start: str) -> str | None:
        """Ask for a branch name; the AI tidies up whatever was typed. Returns a valid, unused name."""
        typed = await self.push_screen_wait(AskTextScreen(
            "New branch", placeholder="e.g. login page, or fix footer on mobile",
            hint=f"It starts from {escape(start)}. Type a name or a few words about what it's for"
                 + (", and the AI will tidy it up." if ai.available() else ".")))
        if not typed:
            return None
        existing = git.branches(s.root)
        name = git.tidy_branch_name(typed)
        if ai.available():
            suggestion = await self.with_ai(ai.suggest_branch_name, typed, s, start, existing,
                                            doing="tidying up the branch name", event="ai_branch_name")
            better = git.tidy_branch_name(suggestion.name) if suggestion else ""
            if (better and better != name and better not in existing
                    and git.valid_branch_name(s.root, better)):
                final = await self.push_screen_wait(AskTextScreen(
                    "New branch", value=better,
                    hint=(f"You typed “{escape(typed)}”. The AI suggests this "
                          f"({escape(suggestion.reason.rstrip('.'))}). Edit it, or press Enter to use it.")))
                self.log_.event("ai_branch_name_choice", choice="cancelled" if not final
                                else "kept" if final.strip() == better else "edited")
                if not final:
                    return None
                name = git.tidy_branch_name(final)
        if not name:
            self.notify("That name has no letters or numbers in it. Try again.", severity="warning")
            return None
        if name in existing:
            self.notify(f"There's already a branch called '{name}'.", severity="warning")
            return None
        if not git.valid_branch_name(s.root, name):
            self.notify(f"'{name}' can't be a branch name. Try letters, numbers and dashes.",
                        severity="warning")
            return None
        return name

    async def ai_setup(self, rejected: bool = False) -> bool:
        """Ask for an Anthropic API key and save it. Returns True if one was saved."""
        if ai.key_from_env():
            await self.push_screen_wait(TextScreen("AI key rejected", (
                "Your ANTHROPIC_API_KEY environment variable was rejected. Fix or remove it "
                "(e.g. in ~/.zshrc), then restart gitly.")))
            return False
        lead = ("That key was rejected. Paste a new one." if rejected
                else "The AI features need an Anthropic API key. Everything else works without it.")
        key = await self.push_screen_wait(AskTextScreen(
            "Turn on AI", placeholder="sk-ant-...", password=True,
            hint=(f"{lead}\nGet one at [b]platform.claude.com[/] → API Keys, then paste it here. "
                  f"It's saved on this computer only ({escape(str(ai.key_file()).replace(str(Path.home()), '~'))}).")))
        if not key:
            return False
        key = "".join(key.split())  # pasted keys sometimes pick up spaces or line breaks
        if not key.startswith("sk-ant-"):
            self.notify("That doesn't look like an Anthropic API key (they start with sk-ant-).",
                        severity="warning", timeout=8)
            return False
        try:
            ai.save_key(key)
        except OSError as e:
            self.notify(f"Couldn't save the key: {e}", severity="error", timeout=8)
            return False
        self.log_.event("ai_key_saved", replaced=rejected)
        self._ai_failures = 0
        self.notify("Key saved. AI features are on.")
        return True

    async def with_ai(self, fn, *args, doing: str, event: str):
        """Run one AI call behind a "Thinking…" dialog. Returns its result, or None on failure."""
        self.push_screen(ThinkingScreen(doing))
        try:
            result, tel = await asyncio.to_thread(fn, *args)
        except ai.AIUnavailable as e:
            failed = e
        else:
            failed = None
        finally:
            self.pop_screen()
        if failed is not None:
            self.log_.event("ai_error", feature=event, message=str(failed))
            if isinstance(failed, ai.AIKeyRejected):
                if await self.ai_setup(rejected=True):  # the saved key was refused: offer a new one, retry once
                    return await self.with_ai(fn, *args, doing=doing, event=event)
            else:
                self.notify(str(failed), severity="error", timeout=8)
            return None
        self.log_.event(event, **tel)
        return result

    async def explain_flow(self, s: git.RepoState) -> None:
        if not ai.available() and not await self.ai_setup():
            return
        summary = await self.with_ai(ai.summarize_changes, s, doing="reading your changes",
                                     event="ai_summary")
        if summary is None:
            return
        body = escape(summary.overview)
        if summary.points:
            body += "\n\n" + "\n".join(f"• {escape(p)}" for p in summary.points)
        if not ai.shares_code():
            body += "\n\n[dim]Based on file names only (GITLY_AI_SEND_CODE=0).[/]"
        await self.push_screen_wait(TextScreen("What did I change?", body))

    async def ask_flow(self, preset: str = "", error: str | None = None) -> None:
        if not ai.available() and not await self.ai_setup():
            return
        if self.repo is None:
            self.notify("Start tracking this folder first.", severity="warning")
            return
        q = await self.push_screen_wait(AskTextScreen(
            "Ask in plain English", value=preset,
            placeholder="e.g. I committed to the wrong branch",
            hint="Describe what you want or what went wrong. You'll see the exact commands before anything runs."))
        if not q:
            return
        self.log_.event("ai_ask", question=q, with_error=error is not None)
        plan = await self.with_ai(ai.ask, q, self.repo, error, doing="asking the AI for a plan",
                                  event="ai_plan")
        if plan is not None:
            await self.execute(plan)

    # ---- running plans

    async def execute(self, plan: Plan) -> None:
        self.busy = True
        try:
            await self._execute(plan)
        finally:
            self.busy = False
            self.refresh_state()  # back on the menu, the cursor moves to the new next step

    async def _markers_ok(self, plan: Plan) -> bool:
        """Before a plan finishes a merge: stop if conflict markers are still in the files."""
        if not any(st.args[:1] == ["commit"] or "--continue" in st.args for st in plan.steps):
            return True
        s = await asyncio.to_thread(git.state, self.cwd)
        if s is None or not (s.conflicts or s.merging or s.rebasing or s.reverting):
            return True
        found = await asyncio.to_thread(precheck.leftover_markers, s)
        if not found:
            return True
        n = len(found)
        shown = "\n".join(f"  [b]{escape(p)}[/]  [dim]line {line}[/]" for p, line in found[:8])
        more = f"\n  [dim]…and {n - 8} more[/]" if n > 8 else ""
        choice = await self.push_screen_wait(ChooseScreen(
            f"Conflict markers are still in {n} file{'s' if n != 1 else ''}",
            [("back", "Go back and fix them  [dim](recommended)[/]"),
             ("anyway", "Finish anyway")],
            hint=f"{shown}{more}\n\nThese files still have <<<<<<< or >>>>>>> lines. Finishing now "
                 "would save them as they are, and the code probably won't run. Open each file, "
                 "keep the version you want, and delete the marker lines."))
        self.log_.event("conflict_markers", files=n, choice=choice or "back")
        return choice == "anyway"

    async def _execute(self, plan: Plan) -> None:
        log = self.log_
        if not await self._markers_ok(plan):
            return
        log.event("plan_shown", title=plan.title, source=plan.source, risk=plan.risk,
                  commands=[st.args for st in plan.steps], blocked=[r for _, r in plan.blocked])
        ok = await self.push_screen_wait(PlanScreen(plan))
        log.event("plan_decision", title=plan.title, source=plan.source, run=bool(ok))
        if not ok or not plan.runnable:
            return

        root = self.repo.root if self.repo else self.cwd
        if plan.risk == safety.DANGER:
            b = await asyncio.to_thread(git.make_backup, root, plan.title)
            if b:
                log.event("backup", id=b.id, wip=bool(b.wip))

        last: git.Result | None = None
        for st in plan.steps:
            if st.gitignore:
                last = await asyncio.to_thread(git.append_gitignore, root, st.gitignore)
            else:
                last = await asyncio.to_thread(git.run, st.args, root)
            log.event("step", args=st.args, gitignore=len(st.gitignore), code=last.code,
                      seconds=round(last.seconds, 2), source=plan.source)
            if not last.ok:
                break

        if last is not None and last.ok:
            tail = last.output.splitlines()[-1:] if last.output else []
            self.notify(escape(tail[0]) if tail else "", title=f"✓ {plan.title}", timeout=5)
            return

        # Failure: diagnose and offer fixes
        assert last is not None
        self.refresh_state()
        s = self.repo
        diag = rescue.diagnose(last, s) if s else None
        log.event("failure", args=last.args, code=last.code, rescue=diag.title if diag else None,
                  stderr=last.stderr[-500:])
        if diag and not diag.fixes and not diag.needs_input and "nothing to commit" in last.output:
            self.notify(diag.explanation, title=diag.title)
            return
        choice = await self.push_screen_wait(ProblemScreen(last, diag, ai.available()))
        log.event("rescue_choice", choice=choice)
        if choice is None:
            return
        if choice.startswith("fix:"):
            await self._execute(diag.fixes[int(choice[4:])])
        elif choice == "ask":
            self.busy = False
            await self.ask_flow(preset=f"`{git.display(last.args)}` failed. What should I do?",
                                error=last.output)
        elif choice == "input" and diag:
            if diag.needs_input == "remote_url":
                p = await self.connect_flow()
                if p:
                    await self._execute(p)
            elif diag.needs_input == "identity":
                name = await self.push_screen_wait(AskTextScreen("Your name", placeholder="Ada Lovelace"))
                email = name and await self.push_screen_wait(AskTextScreen(
                    "Your email", placeholder="you@example.com",
                    hint="Use the email on your GitHub account so your commits link to you."))
                if name and email:
                    await self._execute(rescue.identity_plan(name, email))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="gitly", description="Git without typing commands.")
    parser.add_argument("path", nargs="?", default=os.getcwd(), help="project folder (default: here)")
    parser.add_argument("--study", metavar="PARTICIPANT_ID",
                        help="log actions to ~/.gitly/logs for a usability study")
    args = parser.parse_args()
    Gitly(str(Path(args.path).resolve()), participant=args.study).run()
