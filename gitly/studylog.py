"""Opt-in local event log for usability studies.

Enabled with `gitly --study P07` (or GITLY_LOG=1). Writes one JSON object per
line to ~/.gitly/logs/<participant>-<timestamp>.jsonl. Nothing is uploaded.
File contents and diffs are never logged; commands and timings are.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class StudyLog:
    def __init__(self, participant: str | None):
        self.enabled = participant is not None or os.environ.get("GITLY_LOG") == "1"
        self.participant = participant or "anon"
        self.path: Path | None = None
        self._t0 = time.monotonic()
        if self.enabled:
            d = Path(os.environ.get("GITLY_LOG_DIR", Path.home() / ".gitly" / "logs"))
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / f"{self.participant}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    def event(self, kind: str, **data) -> None:
        if not self.enabled or self.path is None:
            return
        rec = {"t": round(time.monotonic() - self._t0, 3), "ts": time.time(),
               "participant": self.participant, "event": kind, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
