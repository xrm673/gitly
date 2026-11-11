"""gitly's terminal UI (Textual)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option

from . import actions, ai, git, rescue, safety
from .plan import Plan
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

    def __init__(self, title: str, placeholder: str = "", hint: str = "", value: str = ""):
        super().__init__()
        self.title_text, self.placeholder, self.hint, self.value = title, placeholder, hint, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/]", classes="dialog-title")
            if self.hint:
                yield Static(self.hint, classes="muted")
            yield Input(value=self.value, placeholder=self.placeholder)
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
                if p.steps:
                    yield Static("[b]This will run:[/]")
                for i, st in enumerate(p.steps, 1):
                    cmd = escape(git.display(st.args))
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
    #files { width: 1fr; height: 100%; border: round $panel; padding: 0 1; }
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
            yield VerticalScroll(Static(id="filelist"), id="files")
            yield OptionList(id="menu")
        yield Footer()

    def on_mount(self) -> None:
        self.log_.event("start", cwd=self.cwd, ai_available=ai.available(), model=ai.MODEL)
        self.home.query_one("#files").border_title = "Changes"
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
            return

        top.update(f"[b]gitly[/]  [dim]{escape(s.root)}[/]")
        summary.update(s.summary())

        warn = ""
        if s.conflicts:
            warn = (f"⚠ {len(s.conflicts)} file(s) have conflicts. Edit them to keep the right "
                    "version (remove the <<<<<<< >>>>>>> markers), then choose “I fixed the conflicts”.")
        elif s.merging or s.rebasing:
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
        if s.conflicts or s.merging or s.rebasing:
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
            suggestion = ""
            if ai.available():
                suggestion = await self.with_ai(
                    ai.suggest_commit_message, s, doing="writing a description of your changes",
                    event="ai_commit_message") or ""
            hint = "Describe what you changed in a few words. This label helps you find this save later."
            if suggestion:
                hint = "The AI suggested this from your changes. Edit it, or press Enter to keep it."
            msg = await self.push_screen_wait(AskTextScreen(
                "Save my work", placeholder="e.g. add login page", hint=hint, value=suggestion))
            if suggestion:
                self.log_.event("ai_commit_message_choice",
                                choice="cancelled" if not msg else "kept" if msg == suggestion else "edited")
            if msg:
                plan = actions.save_work(s, msg)
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
            name = await self.push_screen_wait(AskTextScreen(
                "New branch name", placeholder="e.g. dark-mode", hint="Short, no spaces."))
            if name:
                return actions.new_branch(name.replace(" ", "-"))
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

    async def ai_setup_help(self) -> None:
        await self.push_screen_wait(TextScreen("AI isn't set up", (
            "To use the AI features, set an Anthropic API key:\n\n"
            "  [b]export ANTHROPIC_API_KEY=sk-ant-...[/]\n\n"
            "then restart gitly. Everything else works without it.")))

    async def with_ai(self, fn, *args, doing: str, event: str):
        """Run one AI call behind a "Thinking…" dialog. Returns its result, or None on failure."""
        self.push_screen(ThinkingScreen(doing))
        try:
            result, tel = await asyncio.to_thread(fn, *args)
        except ai.AIUnavailable as e:
            self.log_.event("ai_error", feature=event, message=str(e))
            self.notify(str(e), severity="error", timeout=8)
            return None
        finally:
            self.pop_screen()
        self.log_.event(event, **tel)
        return result

    async def explain_flow(self, s: git.RepoState) -> None:
        if not ai.available():
            await self.ai_setup_help()
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
        if not ai.available():
            await self.ai_setup_help()
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
            last = await asyncio.to_thread(git.run, st.args, root)
            log.event("step", args=st.args, code=last.code, seconds=round(last.seconds, 2),
                      source=plan.source)
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
