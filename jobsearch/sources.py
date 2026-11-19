"""Fetchers for each job source. Every fetcher returns a list of Job objects.

All sources here are public JSON endpoints meant for programmatic access
(company ATS job-board APIs, a GitHub-hosted listings file, and Adzuna's API),
so no HTML scraping or bot-evasion is involved.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

UA = {"User-Agent": "job-digest/1.0 (personal job alert script)"}


@dataclass
class Job:
    source: str
    job_id: str
    company: str
    title: str
    locations: list[str]
    url: str
    posted: datetime | None = None
    description: str = ""          # plain text, may be empty
    remote: bool = False
    tags: list[str] = field(default_factory=list)   # filled in by filters
    category: str = ""                              # filled in by filters
    # Greenhouse list endpoint has no description; filters can call this lazily.
    _describe: object = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company.lower()}:{self.job_id}"

    @property
    def fuzzy_key(self) -> str:
        """company|title, robust to spacing and suffixes, so "Longitude Labs" (Simplify)
        and "Longitudelabs" (Ashby slug) collapse to the same job."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        company = re.sub(r"[^a-z0-9]", "", self.company.lower())
        for _ in range(2):
            company = re.sub(r"(inc|llc|ltd|corp|co|ai|health|technologies|labs|hq)$", "", company) or company
        return f"{company}|{norm(self.title)}"

    @property
    def legacy_fuzzy_key(self) -> str:
        """Key format used before Nov 2026; still checked so old entries stay 'seen'."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        company = re.sub(r"\b(inc|llc|ltd|corp|co|ai|health|technologies|labs|hq)\b",
                         "", norm(self.company))
        return f"{' '.join(company.split())}|{norm(self.title)}"

    def get_description(self) -> str:
        if not self.description and callable(self._describe):
            try:
                self.description = self._describe()
            except Exception:
                self.description = ""
        return self.description


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:  # milliseconds
                value /= 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


# --------------------------------------------------------------------------- #
# SimplifyJobs New-Grad-Positions (community-maintained, entry-level only)
# --------------------------------------------------------------------------- #
SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
                "dev/.github/scripts/listings.json")


def fetch_simplify() -> list[Job]:
    jobs = []
    for x in _get_json(SIMPLIFY_URL, timeout=90):
        if not (x.get("active") and x.get("is_visible", True)):
            continue
        locs = [l.replace("NYC", "New York, NY") for l in x.get("locations", [])]
        jobs.append(Job(
            source="Simplify New Grad",
            job_id=str(x["id"]),
            company=x.get("company_name", "?"),
            title=x.get("title", "?").replace("�", "–"),
            locations=locs,
            url=x.get("url", ""),
            posted=_ts(x.get("date_posted")),
            remote=any("remote" in l.lower() for l in locs),
            tags=["new-grad list"],
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Greenhouse / Lever / Ashby public job-board APIs
# --------------------------------------------------------------------------- #
def _greenhouse_one(slug: str) -> list[Job]:
    d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    out = []
    for j in d.get("jobs", []):
        jid = j["id"]
        loc = (j.get("location") or {}).get("name", "")
        out.append(Job(
            source="Greenhouse",
            job_id=str(jid),
            company=j.get("company_name") or slug,
            title=j.get("title", "?"),
            locations=[p.strip() for p in re.split(r"[;|]", loc) if p.strip()] or [loc],
            url=j.get("absolute_url", ""),
            posted=_ts(j.get("first_published") or j.get("updated_at")),
            remote="remote" in loc.lower(),
            _describe=(lambda s=slug, i=jid: _strip_html(
                _get_json(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs/{i}")
                .get("content", ""))),
        ))
    return out


def _lever_one(slug: str) -> list[Job]:
    d = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        if (cats.get("commitment") or "").lower().startswith("intern"):
            continue
        out.append(Job(
            source="Lever",
            job_id=j["id"],
            company=slug.capitalize(),
            title=j.get("text", "?"),
            locations=locs,
            url=j.get("hostedUrl", ""),
            posted=_ts(j.get("createdAt")),
            description=" ".join(filter(None, [j.get("descriptionPlain"),
                                               j.get("additionalPlain")]))
                        + " " + " ".join(_strip_html(l.get("content", ""))
                                         for l in j.get("lists", [])),
            remote=(j.get("workplaceType") == "remote"),
        ))
    return out


def _ashby_one(slug: str) -> list[Job]:
    d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in d.get("jobs", []):
        if not j.get("isListed", True) or j.get("employmentType") == "Intern":
            continue
        locs = [j.get("location") or ""] + [
            s.get("location", "") for s in j.get("secondaryLocations") or []]
        out.append(Job(
            source="Ashby",
            job_id=j["id"],
            company=slug.capitalize(),
            title=j.get("title", "?"),
            locations=[l for l in locs if l],
            url=j.get("jobUrl", ""),
            posted=_ts(j.get("publishedAt")),
            description=j.get("descriptionPlain") or "",
            remote=bool(j.get("isRemote")),
        ))
    return out


def fetch_ats(kind: str, slugs: list[str], errors: list[str]) -> list[Job]:
    fn = {"greenhouse": _greenhouse_one, "lever": _lever_one, "ashby": _ashby_one}[kind]

    def safe(slug):
        try:
            return fn(slug)
        except Exception as e:  # one dead board shouldn't sink the run
            errors.append(f"{kind}/{slug}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=12) as ex:
        return [job for batch in ex.map(safe, slugs) for job in batch]


# --------------------------------------------------------------------------- #
# Adzuna aggregator API (optional; needs free key)
# --------------------------------------------------------------------------- #
def fetch_adzuna(cfg: dict, errors: list[str]) -> list[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        errors.append("adzuna: enabled in config but ADZUNA_APP_ID/ADZUNA_APP_KEY missing in .env")
        return []
    jobs = []
    for what in cfg.get("queries", []):
        for where in cfg.get("where", []):
            q = urllib.parse.urlencode({
                "app_id": app_id, "app_key": app_key, "what": what, "where": where,
                "max_days_old": cfg.get("max_days_old", 3), "results_per_page": 50,
                "content-type": "application/json"})
            try:
                d = _get_json(f"https://api.adzuna.com/v1/api/jobs/us/search/1?{q}")
            except Exception as e:
                errors.append(f"adzuna '{what}' @ {where}: {e}")
                continue
            for r in d.get("results", []):
                jobs.append(Job(
                    source="Adzuna",
                    job_id=str(r.get("id")),
                    company=(r.get("company") or {}).get("display_name", "?"),
                    title=_strip_html(r.get("title", "?")),
                    locations=[(r.get("location") or {}).get("display_name", where)],
                    url=r.get("redirect_url", ""),
                    posted=_ts(r.get("created")),
                    description=_strip_html(r.get("description", "")),
                ))
    return jobs


def fetch_all(cfg: dict) -> tuple[list[Job], list[str]]:
    src = cfg["sources"]
    errors: list[str] = []
    jobs: list[Job] = []
    if src.get("simplify_new_grad"):
        try:
            jobs += fetch_simplify()
        except Exception as e:
            errors.append(f"simplify: {e}")
    for kind in ("greenhouse", "lever", "ashby"):
        if src.get(kind):
            jobs += fetch_ats(kind, src[kind], errors)
    if (src.get("adzuna") or {}).get("enabled"):
        jobs += fetch_adzuna(src["adzuna"], errors)
    return jobs, errors
