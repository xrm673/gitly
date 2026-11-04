# gitly

Git without typing commands. gitly is a terminal app for people who build with
AI tools and don't want to memorize git: arrow-key menus for everyday work,
plain-English explanations when something goes wrong, and an AI helper that
always shows you the exact command before anything runs.

```
 gitly  ~/projects/my-app
 You're on main. 3 files changed, not saved yet. GitHub has 2 commits you don't.

 ╭ Changes ──────────────────────╮ ╭ What do you want to do? ───────────╮
 │ modified  app.py              │ │ ❯ Save my work  (3 files)          │
 │ modified  styles.css          │ │   Upload to GitHub                 │
 │ new       README.md           │ │   Get latest  (2 waiting)          │
 │                               │ │   Branches…                        │
 │                               │ │   Undo…                            │
 │                               │ │   History                          │
 │                               │ │   ──────────────────               │
 │                               │ │   Ask in plain English  ?          │
 ╰───────────────────────────────╯ ╰────────────────────────────────────╯
```

## Install

Requires Python 3.10+ and git.

```bash
pipx install git+https://github.com/xrm673/gitly
# or, from a clone:
pipx install .
```

For development:

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/gitly            # run in the current folder
.venv/bin/gitly ~/my-app   # or point it at a project
```

To turn on **Ask in plain English**, set an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GITLY_MODEL=claude-haiku-5-5   # optional; this is the default
```

Everything except the Ask box works without a key.

## How it works

Every action, whether it comes from a menu, an error fix, or the AI, becomes a
**plan**: a list of real git commands, each with a one-sentence explanation and
a risk level. You see the plan and confirm it before anything runs.

| Risk | Examples | What gitly does |
|---|---|---|
| safe | `push`, `switch`, `commit -m` | Enter runs it |
| careful | `pull`, `reset --soft`, `branch --delete` | Enter runs it, marked yellow |
| risky | `reset --hard`, `push --force`, `clean` | You type `yes`; gitly saves a backup first |

**Backups.** Before any risky plan, gitly stores the current commit and any
uncommitted work under `refs/gitly/backup/…` (via `git stash create`, which
doesn't touch your files). Get it back with **Undo → Restore a gitly backup**.

**Error rescue.** When git fails, gitly matches the error against common
problems (push rejected, merge conflict, no upstream, divergent branches,
unsaved changes in the way, no remote, auth failure, missing name/email,
nothing to commit) and offers a one-click fix. Anything it doesn't recognize
can be sent to the AI along with the error output.

**AI guardrails.** The model returns structured JSON (a list of git argument
lists), never shell text. Commands run without a shell. gitly refuses
non-git commands, global options like `-c`/`-C`, interactive or editor flags,
config keys other than `user.name`/`user.email`, and flags that run other
programs (`--exec`, `--upload-pack`, `rebase -x`, …). See `gitly/safety.py`.

## Usability studies

```bash
gitly --study P07
```

writes one JSON event per line to `~/.gitly/logs/P07-<timestamp>.jsonl`: menu
choices, plans shown, whether they were run or cancelled, each command's exit
code and duration, failures and which rescue was offered, AI questions, latency
and token counts. File contents are never logged and nothing is uploaded.
Override the folder with `GITLY_LOG_DIR`.

## Code map

| File | What's in it |
|---|---|
| `gitly/app.py` | Textual UI: home screen, menus, plan confirmation, rescue screens |
| `gitly/git.py` | Runs the real `git` CLI; parses status; backups |
| `gitly/actions.py` | The no-AI menu plans |
| `gitly/rescue.py` | Error pattern → explanation + fixes |
| `gitly/safety.py` | Risk levels and blocked commands |
| `gitly/ai.py` | Plain-English request → structured plan (Anthropic API) |
| `gitly/studylog.py` | Opt-in JSONL event log |

## Tests

```bash
.venv/bin/pytest
```
