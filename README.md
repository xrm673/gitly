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

Everything except the AI features works without a key.

## How it works

Every action, whether it comes from a menu, an error fix, or the AI, becomes a
**plan**: a list of real git commands, each with a one-sentence explanation and
a risk level. You see the plan and confirm it before anything runs.

| Risk | Examples | What gitly does |
|---|---|---|
| safe | `push`, `switch`, `commit -m` | Enter runs it |
| careful | `pull`, `reset --soft`, `branch --delete` | Enter runs it, marked yellow |
| risky | `reset --hard`, `push --force`, `clean` | You type `yes`; gitly saves a backup first |

**Pre-save check.** Before **Save my work**, gitly looks at what the save would
include and warns about:

- secrets files: `.env*` (not `.env.example`), `*.pem`, `*.key`, `id_rsa`,
  `credentials.json`, `.npmrc`, …
- API keys pasted into code (Anthropic, OpenAI, Stripe live, AWS, GitHub,
  Slack, Google, private key blocks), with the line number
- generated folders and clutter: `node_modules/`, `.venv/`, `__pycache__/`,
  `.next/`, `.DS_Store`, …
- files over 50 MB (GitHub rejects files over 100 MB)

The recommended fix, **Leave these out and save the rest**, is a normal plan you
review first: new files are added to `.gitignore` so the warning doesn't come
back, and anything `.gitignore` can't help with (a key inside `app.py`, a
secrets file that was already committed) is left out of this one save with a
pathspec. Files on disk are never deleted. Adding lines to `.gitignore` is the
only plan step that isn't a git command; the AI can't produce it. The check is
plain pattern matching, so it's instant and works without an API key. See
`gitly/precheck.py` and `gitly/secretscan.py`.

**Backups.** Before any risky plan, gitly stores the current commit and any
uncommitted work under `refs/gitly/backup/…` (via `git stash create`, which
doesn't touch your files). Get it back with **Undo → Restore a gitly backup**.

**Error rescue.** When git fails, gitly matches the error against common
problems (push rejected, merge conflict, no upstream, divergent branches,
unsaved changes in the way, no remote, auth failure, missing name/email,
nothing to commit) and offers a one-click fix. Anything it doesn't recognize
can be sent to the AI along with the error output.

**AI save descriptions.** With a key set, **Save my work** reads your unsaved
changes and pre-fills a one-line description. Edit it or press Enter to keep it.
If the AI is unreachable, the box is simply empty and you type your own.

**What did I change?** Shows up in the menu whenever you have unsaved changes.
It gives a plain-English summary of the changes and flags anything that looks
accidental (leftover debug code, a deleted feature, a secrets file).

**What gets sent to the AI.** Both features send the unsaved diff to the
Anthropic API: tracked edits plus new files, capped at about 3,000 characters
per file and 12,000 in total. Files that look like secrets (`.env*`, `*.pem`,
`*.key`, `id_rsa`, `.npmrc`, `credentials.json`, …) are listed by name only,
API keys found in other files are replaced with `[secret removed by gitly]`,
and files you chose to leave out of the save aren't sent at all. Set `GITLY_AI_SEND_CODE=0` to send file names
only, with no contents at all. See `working_diff()` in `gitly/git.py`.

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
and token counts, and whether an AI-suggested save description was kept, edited
or cancelled. File contents are never logged and nothing is uploaded.
Override the folder with `GITLY_LOG_DIR`.

## Code map

| File | What's in it |
|---|---|
| `gitly/app.py` | Textual UI: home screen, menus, plan confirmation, rescue screens |
| `gitly/git.py` | Runs the real `git` CLI; parses status; backups |
| `gitly/actions.py` | The no-AI menu plans |
| `gitly/rescue.py` | Error pattern → explanation + fixes |
| `gitly/precheck.py` | Pre-save check: findings + the "leave these out" plan |
| `gitly/secretscan.py` | Secret file names and API key patterns |
| `gitly/safety.py` | Risk levels and blocked commands |
| `gitly/ai.py` | AI features (Anthropic API): Ask → plan, save descriptions, change summaries |
| `gitly/studylog.py` | Opt-in JSONL event log |

## Tests

```bash
.venv/bin/pytest
```
