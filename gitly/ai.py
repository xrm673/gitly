"""The "Ask" box: plain-English request -> a structured, reviewable Plan.

The model never executes anything. It returns JSON (enforced by structured
outputs), which we convert to a Plan; every step then goes through
safety.blocked_reason() and is shown to the user before it can run.
"""

from __future__ import annotations

import os
import time

from pydantic import BaseModel, Field

from .git import RepoState, recent_log
from .plan import Plan, Step

MODEL = os.environ.get("GITLY_MODEL", "claude-haiku-5-5")

SYSTEM = """You are the assistant inside gitly, a friendly terminal app that helps \
people who are new to git (often "vibe coders" who build with AI tools) manage \
their projects without typing commands.

The user describes what they want in plain English. You reply with:
- answer: 1-3 short sentences in plain, friendly English. No jargon without a \
quick definition. If they asked a question, answer it here.
- steps: the git commands that accomplish the goal, in order. Each step has \
`args` (the arguments AFTER "git", one list item per argument, e.g. \
["commit", "-m", "fix login"]) and `explanation` (one plain-English sentence). \
Leave steps empty if no command is needed or the request isn't about git.

Rules:
- Only git commands. Never shell commands, pipes, or other programs.
- Never use interactive flags (-i, -p, --edit) or anything that opens an editor; \
always pass commit messages with -m.
- Prefer the safest command that does the job: keep the user's work \
(e.g. reset --soft over --hard, revert over rewriting pushed history, \
--force-with-lease over --force, switch/restore over checkout).
- Use the repo state below: real branch names, real file paths. Don't invent remotes.
- If the request is ambiguous or risky, say so in `answer` and give the \
conservative option.
- Keep plans short: usually 1-4 steps."""


class AIStep(BaseModel):
    args: list[str] = Field(description="Arguments after 'git'")
    explanation: str


class AIPlan(BaseModel):
    answer: str
    title: str = Field(description="2-5 word name for this plan, e.g. 'Move commit to new branch'")
    steps: list[AIStep]


class AIUnavailable(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def ask(question: str, s: RepoState, error: str | None = None) -> tuple[Plan, dict]:
    """Returns (plan, telemetry). Raises AIUnavailable with a user-facing message."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise AIUnavailable("The anthropic package isn't installed.") from e

    context = f"<repo_state>\n{s.as_context()}\n</repo_state>\n"
    log = recent_log(s.root, 8)
    if log:
        context += "<recent_commits>\n" + "\n".join(log) + "\n</recent_commits>\n"
    if error:
        context += f"<last_error>\n{error[-3000:]}\n</last_error>\n"
    content = f"{context}\n<request>\n{question}\n</request>"

    client = anthropic.Anthropic(max_retries=1, timeout=60)
    start = time.monotonic()
    try:
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low"},  # fast replies matter more than depth here
            output_format=AIPlan,
        )
    except anthropic.AuthenticationError as e:
        raise AIUnavailable("Your Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise AIUnavailable("Too many AI requests right now. Wait a minute and try again.") from e
    except anthropic.APIConnectionError as e:
        raise AIUnavailable("Couldn't reach the AI service. Check your internet connection.") from e
    except anthropic.APIStatusError as e:
        raise AIUnavailable(f"The AI service returned an error ({e.status_code}).") from e

    telemetry = {
        "model": MODEL,
        "latency_s": round(time.monotonic() - start, 2),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
    }
    if resp.stop_reason == "refusal":
        raise AIUnavailable("The AI declined to help with that request.")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise AIUnavailable("The AI's answer got cut off. Try asking more specifically.")

    out: AIPlan = resp.parsed_output
    # The model sometimes includes the leading "git"; strip it.
    steps = [Step(st.args[1:] if st.args[:1] == ["git"] else st.args, st.explanation)
             for st in out.steps]
    plan = Plan(out.title or "Suggested plan", steps, source="ai", note=out.answer)
    telemetry["suggested"] = [st.args for st in steps]
    return plan, telemetry
