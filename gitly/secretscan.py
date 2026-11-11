"""Recognize secrets: files that usually hold them, and keys pasted into code.

Used by the pre-save check (precheck.py) and to redact what the AI sees
(git.working_diff). Patterns favor precision: a false alarm on every save
teaches people to click through the warning.
"""

from __future__ import annotations

import re

# Files whose contents should never be saved or sent to the AI.
# Template files like .env.example are meant to be shared, so they don't count.
SECRET_FILE = re.compile(
    r"(^|/)(\.env(?!\.(example|sample|template|dist)$)(\..*)?"
    r"|.*\.(pem|key|p12|pfx|keystore|jks)|id_(rsa|dsa|ecdsa|ed25519)"
    r"|\.npmrc|\.pypirc|\.netrc|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$",
    re.IGNORECASE,
)

# (plain-English name, pattern). Order matters: the first match names the key.
SECRET_TEXT: list[tuple[str, re.Pattern]] = [
    ("an Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("an OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("a Stripe secret key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def is_secret_file(path: str) -> bool:
    return bool(SECRET_FILE.search(path))


def find(text: str) -> str | None:
    """Plain-English name of the first secret found in `text`, or None."""
    for name, pattern in SECRET_TEXT:
        if pattern.search(text):
            return name
    return None


def redact(text: str) -> str:
    for _, pattern in SECRET_TEXT:
        text = pattern.sub("[secret removed by gitly]", text)
    return text
