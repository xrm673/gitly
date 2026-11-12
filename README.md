# gitly

Git without typing commands. gitly is a terminal app for people who build with
AI tools and don't want to memorize git: arrow-key menus for everyday work,
plain-English explanations when something goes wrong, and an AI helper that
always shows you the exact command before anything runs.

```
  gitly   ~/projects/my-app                                  ✦ AI suggestions on
 You're on login. 2 files changed, not saved yet. In sync with GitHub.

 ╭ Next step ───────────────────────────╮ ╭ What do you want to do? ──────╮
 │ ✦ Save as 2 separate saves           │ │ ❯ ✦ Save as 2 separate saves  │
 │   Your changes touch the login form  │ │     suggested next step       │
 │   and, separately, a payments file.  │ │   ─────────────────────────   │
 ╰─── ✦ suggested by AI · Tab: do it ──╯ │   Save my work  (2 files)     │
 ╭ Changes ─────────────────────────────╮ │   What did I change?          │
 │ modified  login.py                   │ │   Upload to GitHub            │
 │ new       pay.py                     │ │   Get latest                  │
 ╰──────────────────────────────────────╯ │   ─────────────────────────   │
 ╭ Branches ────────────────────────────╮ │   Branches…                   │
 │   main    ●──●──●──●    ✓ on GitHub  │ │   Undo…                       │
 │ ▶ login            ╰──●──◉╌╌◌╌╌◌     │ │   History                     │
 │                      you are here    │ │   ─────────────────────────   │
 ╰─ ● save  ◉ you  ◌ suggested ─────────╯ │   Ask in plain English  ?     │
                                          ╰───────────────────────────────╯
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

The AI features need an Anthropic API key (get one at platform.claude.com →
API Keys). The first time you use one, for example **Ask in plain English**
(`?`), gitly asks you to paste the key and saves it to
`~/.config/gitly/api_key`, readable only by you. If the key is ever rejected,
gitly asks for a new one.

You can use an environment variable instead; it takes priority over the saved key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GITLY_MODEL=claude-sonnet-5-5   # optional; this is the default
export GITLY_AI_SUGGEST=0               # optional; AI only when you ask for it
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

**Next step.** The top of the home screen always says what to do next, and
why: save your work, upload it, get the latest, combine a finished branch,
delete one that's already combined. `Tab` does it from anywhere on the home
screen (you still review the plan first), and since it's the first item in the
menu, `Enter` there does it too; `x` skips it. The same
step is drawn faintly on the branch map, like autocomplete in an editor: `◌`
is a save that doesn't exist yet, a dashed line is where a branch would go.

Without an API key the step comes from the repo state alone (`rules()` in
`gitly/suggest.py`). With a key, the AI also reads your unsaved changes in the
background and refines it, marked with `✦`:

- writes the save description ahead of time, so it's already filled in
- suggests splitting unrelated changes into two or three separate saves
- suggests starting a branch when the changes don't belong where you are
- says whether a saved branch looks finished or still in progress

The AI doesn't write git commands for this. It picks one action from a short
list gitly computed for the current state, and gitly builds the plan; a pick
that isn't on the list, a file list that doesn't match your changes exactly,
or a bad branch name is thrown away and the plain suggestion stays. It runs
once per change, after your files have been still for a few seconds, at most
every 20 seconds, and only while the gitly window has focus. Failures are
silent, and after three in a row it stops for the session. Turn it off with
`GITLY_AI_SUGGEST=0`; the AI features you start yourself keep working.

**Checking GitHub.** When the project has a remote, gitly runs `git fetch`
in the background when it starts and every 5 minutes after that (`r` checks
right away). Fetching only updates what gitly knows about GitHub. It never
changes your files or branches, so when someone else uploads new saves you
get "Get latest" as the next step before your own upload is rejected. The
header shows when it last checked, or "couldn't reach GitHub" when you're
offline or not signed in (it never asks for a password). Turn it off with
`GITLY_AUTO_FETCH=0`.

**Branch map.** The home screen shows your branches as a simple picture,
under your changed files: one row per branch, left = older, right = newer,
starting from the save it grew out of. It shows where you are, your unsaved
changes, which saves are only on GitHub (hollow dots) or not uploaded yet, and
branches that were already combined into main. Each row ends with its latest
save's description, so the dots aren't anonymous. It's a simplified view, not a
full commit graph: each branch shows its last few saves of its own. See
`gitly/graph.py`.

Press `←` to move from the menu to the map (`Esc`, or `→` past a branch's
latest save, goes back). Arrow keys then pick a save; the line under the map says what it is, and the footer
lists the keys.

- `n` starts a new branch from the selected save. On main there's also a faint
  `+ new branch` row under it: move onto it and press `Enter` to start one from
  where you are.
- `Enter` switches to the selected branch.
- `Space` picks up a branch; move to another branch's row and press `Enter` to
  combine it in (`git merge --no-ff`, so there's always one save to undo).
  Before you confirm, gitly predicts conflicts with `git merge-tree` (git 2.38+)
  without touching your files.
- `u` undoes the selected save. Your latest save, if it isn't on GitHub yet,
  can be un-saved (`reset --soft`: its changes become unsaved again). Anything
  else gets a new save that reverses it (`revert`); a merge uses `revert -m 1`,
  with a warning that merging the same branch again won't bring the changes
  back by itself.

Every map action shows a **Now / After** picture next to the exact commands
before anything runs. If you have unsaved changes, gitly checks whether they
are in the way (the other branch, the incoming work, or the undo changes the
same file). If they are, it offers to save them first, with the usual pre-save
check and AI description. If a merge or undo stops on conflicts, the main
screen offers to finish or cancel it. Before finishing, gitly checks the files
for leftover `<<<<<<<` / `>>>>>>>` conflict markers and warns you if any are
still there.

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

**AI branch names.** When you start a branch (menu or map), type a rough name
or a few words, like "fix the footer on phones". The AI turns it into a proper
branch name (`fix/footer-on-phones`): it keeps your words, follows the repo's
prefix style if it has one, and uses your unsaved changes only when they're
about the same thing. You then choose: the AI's name, the name you typed
(tidied), or edit one; if they're the same, gitly doesn't ask twice. Without a key, or
if the AI is down, gitly just tidies what you typed (`Login stuff!` →
`login-stuff`). A suggestion that matches an existing branch is ignored.

**What did I change?** Shows up in the menu whenever you have unsaved changes.
It gives a plain-English summary of the changes and flags anything that looks
accidental (leftover debug code, a deleted feature, a secrets file).

**What gets sent to the AI.** These features, and the background next-step
suggestion, send the unsaved diff to the Anthropic API: tracked edits plus new files, capped at about 3,000 characters
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
and token counts, whether an AI-suggested save description was kept, edited
or cancelled, and each next-step suggestion: shown, accepted or skipped, and
whether it came from the rules or the AI. File contents are never logged and nothing is uploaded.
Override the folder with `GITLY_LOG_DIR`.

## Code map

| File | What's in it |
|---|---|
| `gitly/app.py` | Textual UI: home screen, menus, plan confirmation, rescue screens |
| `gitly/git.py` | Runs the real `git` CLI; parses status; backups |
| `gitly/actions.py` | The no-AI menu plans |
| `gitly/rescue.py` | Error pattern → explanation + fixes |
| `gitly/graph.py` | Branch map: lanes per branch, layout, rendering, the ghost of the next step |
| `gitly/suggest.py` | The next step: rules, what the AI may change, checks on its answer |
| `gitly/precheck.py` | Pre-save check: findings + the "leave these out" plan |
| `gitly/secretscan.py` | Secret file names and API key patterns |
| `gitly/safety.py` | Risk levels and blocked commands |
| `gitly/ai.py` | AI features (Anthropic API): Ask → plan, save descriptions, change summaries, next step |
| `gitly/studylog.py` | Opt-in JSONL event log |

## Tests

```bash
.venv/bin/pytest
```
