"""Weekly company discovery.

Harvests Greenhouse / Lever / Ashby board slugs from the job links in the
SimplifyJobs new-grad and internship lists, probes boards we don't follow yet,
and adds any that currently have a matching role (by title) in a target
location. State lives in data/companies.json:

    {"last_run": "2026-11-12",
     "added":   {"greenhouse:acme": {"name": "Acme", "date": "...", "matches": 3}},
     "checked": {"lever:foo": "2026-11-12"}}       # probed, not added

Boards that didn't qualify are re-probed after `recheck_days`.
"""
from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from .filters import Matcher
from .sources import UA, _ashby_one, _greenhouse_one, _lever_one

LISTS = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
]
SLUG_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([\w.-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I),
}
FETCH = {"greenhouse": _greenhouse_one, "lever": _lever_one, "ashby": _ashby_one}
IGNORE_SLUGS = {"embed", "jobs", "careers", "api"}


def _get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
        return json.load(r)


class Discovery:
    def __init__(self, path: Path, cfg: dict):
        self.path = path
        self.cfg = cfg.get("discovery") or {}
        self.state = {"last_run": "", "added": {}, "checked": {}}
        if path.exists():
            self.state.update(json.loads(path.read_text(encoding="utf-8")))
        self.added_now: list[str] = []

    # Slugs the daily fetch should include, per ATS.
    def slugs(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {k: [] for k in FETCH}
        for key in self.state["added"]:
            kind, slug = key.split(":", 1)
            out.setdefault(kind, []).append(slug)
        return out

    def due(self) -> bool:
        if not self.cfg.get("enabled"):
            return False
        last = self.state.get("last_run")
        every = self.cfg.get("every_days", 7)
        return not last or date.fromisoformat(last) <= date.today() - timedelta(days=every)

    def _candidates(self, known: dict[str, set[str]]) -> list[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for url in LISTS:
            try:
                rows = _get_json(url)
            except Exception:
                continue
            for x in rows:
                link = x.get("url", "")
                for kind, pat in SLUG_PATTERNS.items():
                    m = pat.search(link)
                    if m:
                        slug = m.group(1).lower()
                        if slug not in IGNORE_SLUGS:
                            found.add((kind, slug))
        recheck = (date.today() - timedelta(days=self.cfg.get("recheck_days", 90))).isoformat()
        out = []
        for kind, slug in sorted(found):
            key = f"{kind}:{slug}"
            if slug in known.get(kind, set()) or key in self.state["added"]:
                continue
            if self.state["checked"].get(key, "") > recheck:
                continue
            out.append((kind, slug))
        return out

    def run(self, matcher: Matcher, known: dict[str, set[str]], log) -> list[str]:
        cands = self._candidates(known)
        limit = self.cfg.get("max_probes_per_run", 400)
        # Oldest-checked first so the backlog rotates week to week.
        cands.sort(key=lambda ks: self.state["checked"].get(f"{ks[0]}:{ks[1]}", ""))
        batch = cands[:limit]
        log.info("discovery: %d candidate boards, probing %d", len(cands), len(batch))
        min_matches = self.cfg.get("min_matching_roles", 1)
        today = date.today().isoformat()

        def probe(ks):
            kind, slug = ks
            try:
                jobs = FETCH[kind](slug)
            except Exception:
                return ks, None, 0
            hits = [j for j in jobs if matcher.location_ok(j) and matcher.role_category(j.title)]
            name = jobs[0].company if jobs else slug
            return ks, name, len(hits)

        with ThreadPoolExecutor(max_workers=16) as ex:
            for (kind, slug), name, n in ex.map(probe, batch):
                key = f"{kind}:{slug}"
                if name and n >= min_matches:
                    self.state["added"][key] = {"name": name, "date": today, "matches": n}
                    self.state["checked"].pop(key, None)
                    self.added_now.append(name)
                else:
                    self.state["checked"][key] = today
        self.state["last_run"] = today
        log.info("discovery: added %d companies", len(self.added_now))
        return self.added_now

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1, sort_keys=True), encoding="utf-8")
