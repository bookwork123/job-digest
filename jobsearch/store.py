"""Remembers which postings have already been emailed."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import Job

PRUNE_AFTER_DAYS = 180


class SeenStore:
    def __init__(self, path: Path, fresh: bool = False):
        """fresh=True ignores the saved file (it is only replaced on save())."""
        self.path = path
        self.is_first_run = fresh or not path.exists()
        self.seen: dict[str, str] = {}
        if path.exists() and not fresh:
            self.seen = json.loads(path.read_text(encoding="utf-8"))

    def is_new(self, job: Job) -> bool:
        return (job.key not in self.seen and job.fuzzy_key not in self.seen
                and job.legacy_fuzzy_key not in self.seen)

    def mark(self, jobs: list[Job]) -> None:
        """Record jobs as seen 'now'. Also used to refresh still-open postings,
        so long-lived listings never age out of the store and get re-sent."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for j in jobs:
            self.seen[j.key] = now
            self.seen[j.fuzzy_key] = now

    def save(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
        self.seen = {k: v for k, v in self.seen.items()
                     if datetime.fromisoformat(v) >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.seen, indent=0, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
