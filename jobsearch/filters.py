"""Decide whether a job is a fit: role, location, and seniority."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .sources import Job

YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*years?"
    r"(?:[’']s?)?(?:\s+of)?(?:\s+[\w/&,-]+){0,6}?\s+(?:experience|exp\b)",
    re.I,
)


def _compile(patterns):
    return [re.compile(p, re.I) for p in patterns]


class Matcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.loc = _compile(cfg["locations"]["include"])
        self.remote_ok = cfg["locations"].get("include_remote", False)
        self.roles = {cat: _compile(p) for cat, p in cfg["roles"].items()}
        self.exclude = _compile(cfg["exclude_title"])
        self.entry = _compile(cfg["entry_signals"])
        self.max_years = cfg.get("max_years_experience", 2)
        self.unstated_ok = cfg.get("include_unstated_experience", False)
        st = cfg.get("stretch") or {}
        self.stretch_on = st.get("enabled", False)
        self.stretch_name = st.get("section_name", "Stretch roles")
        self.stretch_max = st.get("max_years_experience", self.max_years)
        self.stretch_age = st.get("max_posting_age_days")

    # -- individual checks ------------------------------------------------- #
    def location_ok(self, job: Job) -> bool:
        text = " ; ".join(job.locations)
        if any(p.search(text) for p in self.loc):
            return True
        return self.remote_ok and (job.remote or "remote" in text.lower())

    def role_category(self, title: str) -> str | None:
        for cat, pats in self.roles.items():
            if any(p.search(title) for p in pats):
                return cat
        return None

    def too_senior(self, title: str) -> bool:
        return any(p.search(title) for p in self.exclude)

    def has_entry_signal(self, title: str) -> bool:
        return any(p.search(title) for p in self.entry)

    def required_years(self, text: str) -> int | None:
        """Largest 'N+ years ... experience' floor in the posting. Using the max
        (not min) avoids '7+ years PM, 2+ years managing people' reading as 2."""
        found = [int(m.group(1)) for m in YEARS_RE.finditer(text or "")]
        found = [n for n in found if n <= 20]
        return max(found) if found else None

    # -- pipeline ---------------------------------------------------------- #
    def prefilter(self, job: Job) -> bool:
        """Cheap checks that need no network."""
        if not self.location_ok(job) or self.too_senior(job.title):
            return False
        cat = self.role_category(job.title)
        if not cat:
            return False
        job.category = cat
        return True

    def seniority_ok(self, job: Job) -> bool:
        """Entry-level check. May fetch a description (Greenhouse)."""
        if "new-grad list" in job.tags:
            return True
        # Always read the stated requirement when there is one: a title like
        # "Product Analyst" looks junior but can still ask for 2+ years.
        yrs = self.required_years(job.get_description())
        if yrs is not None and yrs > self.max_years:
            return self._stretch(job, yrs)
        if self.has_entry_signal(job.title):
            job.tags.append("entry-level title")
            return True
        if yrs is None:
            if self.unstated_ok:
                job.tags.append("experience not stated")
                return True
            return False
        if yrs <= self.max_years:
            job.tags.append(f"{yrs}+ yrs asked" if yrs else "no experience asked")
            return True
        return False

    def _stretch(self, job: Job, yrs: int) -> bool:
        """A little above the experience bar: keep it, in its own section."""
        if not self.stretch_on or yrs > self.stretch_max:
            return False
        if self.stretch_age and job.posted is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.stretch_age)
            if job.posted < cutoff:
                return False
        job.category = self.stretch_name
        job.tags.append(f"{yrs}+ yrs asked")
        return True

    def run(self, jobs: list[Job]) -> list[Job]:
        candidates = [j for j in jobs if self.prefilter(j)]
        with ThreadPoolExecutor(max_workers=12) as ex:
            keep = list(ex.map(self.seniority_ok, candidates))
        return [j for j, k in zip(candidates, keep) if k]
