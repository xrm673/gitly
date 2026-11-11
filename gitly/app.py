"""gitly's terminal UI (Textual)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option

from . import actions, ai, git, graph, precheck, rescue, safety
from .plan import Plan, Step
from .studylog import StudyLog

RISK_BADGE = {
    safety.SAFE: "[b green]safe[/]",
    safety.CAUTION: "[b yellow]careful[/]",
    safety.DANGER: "[b red]risky[/]",
}
STATUS_COLOR = {"new": "green", "deleted": "red", "renamed": "cyan", "conflict": "magenta"}


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
            src = {"ai": "  [dim](suggested by AI, check it)[/]", "rescue": ""}.get(p.source, "")
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
                    blocked = f"\n   [red]✗ blocked: {escape(st.blocked)}[/]" if st.blocked else ""
                    yield Static(f"{i}. [b reverse] {cmd} [/]  {badge}\n   {escape(st.why)}{blocked}",
                                 classes="step")
            if p.blocked:
                yield Static("[red]gitly won't run this plan because a step is blocked.[/]")
                yield Button("Close", id="cancel", variant="default")
            elif not p.steps:
                yield Button("OK", id="cancel", variant="primary")
            else:
                if p.risk == safety.DANGER:
                    yield Static("[red]This can throw away work.[/] gitly will save a backup first "
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
            yield Static(f"[b red]✗[/] [b]{escape(title)}[/]", classes="dialog-title")
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
            yield Static(f"[b yellow]⚠[/] [b]Check {'this' if n == 1 else 'these'} before saving[/]",
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
    BranchMap { border: round $panel; padding: 0 1; min-height: 7; }
    BranchMap:focus { border: round $accent; }
    BranchMap #map-wrap { height: 1fr; scrollbar-size-horizontal: 1; }
    BranchMap #map { width: auto; height: auto; }
    BranchMap #map-detail { height: 1; display: none; text-wrap: nowrap; text-overflow: ellipsis; }
    BranchMap:focus #map-detail { display: block; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._sig: str | None = None  # what the graph was built from; rebuild only when it changes
        self.s: git.RepoState | None = None
        self.g: graph.Graph | None = None
        self.cells: list[list[tuple[int, str]]] = []
        self.sel: tuple[int, int] | None = None  # (lane index, index into its cells)
        self.held: str | None = None  # branch picked up with Space, to combine into another

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="map-wrap"):
            yield Static(id="map")
        yield Static(id="map-detail")

    def on_mount(self) -> None:
        self.border_title = "Branches"
        self.query_one("#map-wrap").can_focus = False  # arrow keys move the selection, not the scroll
        self._titles()

    def _titles(self, focused: bool = False) -> None:
        self.border_subtitle = (graph.LEGEND_SHORT if focused
                                else "tab: pick a save to branch, switch, combine or undo")

    def on_focus(self) -> None:
        self._titles(focused=True)
        self._draw(focused=True)

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
        self.query_one("#map", Static).update(graph.render(self.g, self.s, selected=cell, held=self.held))
        self.query_one("#map-detail", Static).update(self._detail())

    def _detail(self) -> str:
        sel = self.selected()
        if sel is None:
            return "[dim]Nothing to select yet.[/]"
        lane, sha = sel
        if self.held:
            target = (f"Enter brings it into [b]{escape(lane.name)}[/]"
                      if lane.is_branch and lane.name != self.held else "move to another branch's row")
            return (f"[b black on yellow] Carrying {escape(self.held)} [/]  ↑ ↓ {target} · "
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
        lane, sha = self.selected()
        s, g = self.s, self.g
        here = sha == s.head_sha
        c = g.commit(sha)
        label = "where you are" if here else f"the save “{c.subject if c else sha[:7]}”"
        name = await self.app.ask_branch_name(s, label)
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
        s, g, target = self.s, self.g, lane.name
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
            note = "[green]✓ No conflicts expected.[/] gitly checked this without touching your files."
        elif status == "conflict":
            note = (f"[yellow]⚠ Both branches changed {escape(', '.join(files[:5]))} in different ways.[/] "
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
                notes.append("[yellow]Heads up:[/] after undoing a merge, merging the same branch again "
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
    Screen { background: $surface; }
    #top { padding: 1 2 0 2; height: auto; }
    #summary { padding: 0 2; height: auto; }
    #banner { margin: 1 2 0 2; padding: 0 1; height: auto; background: $warning 20%; display: none; }
    #banner.show { display: block; }
    #body { height: 1fr; padding: 1 2; }
    #left { width: 1fr; height: 100%; }
    #files { height: auto; max-height: 40%; border: round $panel; padding: 0 1; }
    #branchmap { height: 1fr; }
    #menu { width: 46; height: 100%; border: round $accent; }
    ModalScreen { align: center middle; background: $background 60%; }
    .dialog { width: 64; max-width: 95%; height: auto; max-height: 90%;
              border: round $accent; background: $panel; padding: 1 2; }
    .dialog.wide { width: 90; }
    .dialog-title { margin-bottom: 1; }
    .dialog OptionList { height: auto; max-height: 16; margin: 1 0; }
    .dialog Input { margin: 1 0; }
    .plan-body { height: auto; max-height: 24; }
    .step { margin: 0 0 1 0; }
    .note { margin-bottom: 1; }
    .preview { margin-bottom: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .muted { color: $text-muted; margin-bottom: 1; }
    .raw { margin-top: 1; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    LoadingIndicator { height: 3; }
    """
    BINDINGS = [
        Binding("question_mark", "ask", "Ask in plain English"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cwd: str, participant: str | None = None):
        super().__init__()
        self.cwd = cwd
        self.repo: git.RepoState | None = None
        self.log_ = StudyLog(participant)
        self.busy = False

    # ---- layout

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        yield Static(id="summary")
        yield Static(id="banner")
        with Horizontal(id="body"):
            with Vertical(id="left"):
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
            self.refresh_state(keep_highlight=True)

    @property
    def home(self):
        """The main screen (App.query_one would search whichever dialog is on top)."""
        return self.screen_stack[0]

    # ---- state → screen

    def refresh_state(self, keep_highlight: bool = False) -> None:
        if not self.is_running or not self.screen_stack:
            return
        self.repo = git.state(self.cwd)
        s = self.repo
        top = self.home.query_one("#top", Static)
        summary = self.home.query_one("#summary", Static)
        banner = self.home.query_one("#banner", Static)
        files = self.home.query_one("#filelist", Static)

        if s is None:
            top.update(f"[b]gitly[/]  [dim]{escape(self.cwd)}[/]")
            summary.update("This folder isn't a git project yet.")
            banner.remove_class("show")
            files.update("[dim]Nothing tracked yet.[/]")
            self._set_menu([("init", "Start tracking this folder"), None,
                            ("ask", "Ask in plain English  [dim]?[/]"), ("quit", "Quit")], keep_highlight)
            self.home.query_one(BranchMap).update_state(None)
            return

        top.update(f"[b]gitly[/]  [dim]{escape(s.root)}[/]")
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
            lines.append(f"[b magenta]conflict[/]  {escape(c)}")
        seen = set()
        for c in s.changes:
            if c.path in seen:
                continue
            seen.add(c.path)
            col = STATUS_COLOR.get(c.status, "yellow")
            lines.append(f"[{col}]{c.status:<9}[/] {escape(c.path)}")
        for u in s.untracked:
            lines.append(f"[green]{'new':<9}[/] {escape(u)}")
        files.update("\n".join(lines) if lines else "[dim]No unsaved changes.[/]")

        menu: list = []
        if s.conflicts or s.merging or s.rebasing or s.reverting:
            menu += [("finish_merge", "[b]I fixed the conflicts, finish[/]"),
                     ("abort_merge", "Cancel combining"), None]
        if s.detached:
            menu += [("branch_here", "[b]Make a branch here[/]"),
                     ("back_to_branch", "Go back to my branch"), None]
        n = len(seen) + len(s.untracked)
        save_hint = f"  [dim]({n} file{'s' if n != 1 else ''})[/]" if n else "  [dim](nothing to save)[/]"
        up_hint = f"  [dim]({s.ahead} new)[/]" if s.ahead else ""
        pull_hint = f"  [dim]({s.behind} waiting)[/]" if s.behind else ""
        menu += [
            ("save", "Save my work" + save_hint),
            *([("explain", "What did I change?")] if s.dirty else []),
            ("upload", "Upload to GitHub" + up_hint),
            ("pull", "Get latest" + pull_hint),
            ("branches", "Branches…"),
            ("undo", "Undo…"),
            ("history", "History"),
        ]
        if not s.remotes:
            menu.append(("connect", "Connect to GitHub…"))
        menu += [None, ("ask", "Ask in plain English  [dim]?[/]"), ("quit", "Quit  [dim]q[/]")]
        self._set_menu(menu, keep_highlight)
        self.home.query_one(BranchMap).update_state(s)

    def _set_menu(self, items: list, keep_highlight: bool) -> None:
        ol = self.home.query_one("#menu", OptionList)
        prev = ol.highlighted_option.id if (keep_highlight and ol.highlighted_option) else None
        ol.set_options([Option(label, id=key) if key else None
                        for key, label in (i if i else (None, None) for i in items)])
        if prev:
            try:
                ol.highlighted = ol.get_option_index(prev)
                return
            except Exception:
                pass
        ol.highlighted = 0

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
        s = self.repo
        if key == "quit":
            self.exit()
            return
        if key == "ask":
            await self.ask_flow()
            return
        if key == "init":
            await self.execute(actions.init_repo())
            return
        if s is None:
            return

        plan: Plan | None = None
        if key == "save":
            if not s.dirty:
                self.notify("Nothing to save: no changes since your last save.")
                return
            plan = await self.prepare_save(s)
        elif key == "explain":
            await self.explain_flow(s)
        elif key == "upload":
            plan = actions.upload(s)
        elif key == "pull":
            plan = actions.get_latest(s)
        elif key == "branches":
            plan = await self.branches_flow(s)
        elif key == "undo":
            plan = await self.undo_flow(s)
        elif key == "history":
            lines = git.recent_log(s.root, 30)
            body = "\n".join(escape(l) for l in lines) or "[dim]No saves yet.[/]"
            await self.push_screen_wait(TextScreen("History (newest first)", body))
        elif key == "connect":
            plan = await self.connect_flow()
        elif key == "finish_merge":
            plan = actions.finish_merge()
        elif key == "abort_merge":
            plan = actions.abort_merge(s)
        elif key == "branch_here":
            plan = actions.branch_here()
        elif key == "back_to_branch":
            plan = actions.back_to_branch()

        if plan:
            await self.execute(plan)

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

    async def prepare_save(self, s: git.RepoState) -> Plan | None:
        """Pre-save check, AI description, and message prompt. Returns the save plan or None."""
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
        suggestion = ""
        if ai.available() and to_save.dirty:
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
            self.refresh_state(keep_highlight=True)

    async def _execute(self, plan: Plan) -> None:
        log = self.log_
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
