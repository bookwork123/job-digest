"""Turn the plain-language preferences in config.json into the full matcher config.

config.json holds what a person cares about, in plain words:

    {"locations": ["New York", "Chicago"], "include_remote": true,
     "roles": {"Data": ["data analyst", "data scien*"]},
     "experience": "entry"}

build_config() expands that into the regex patterns, seniority rules and source
settings the rest of the code uses. Anything under "advanced" in config.json
overrides the generated values, and a config in the older all-regex format
(where "locations" is an object) passes through unchanged.

"extra_companies" adds job boards to the built-in starter list, by the slug in
the board's URL (boards.greenhouse.io/<slug>, jobs.lever.co/<slug>,
jobs.ashbyhq.com/<slug>).

Phrase rules (locations, roles, exclude_words):
  * matching ignores case, and only matches whole words
  * "*" is a wildcard for the rest of a word:  "product manag*"
  * "re:" at the start means the rest is a raw regular expression
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# Phrase -> regex
# --------------------------------------------------------------------------- #
LOCATION_ALIASES = {
    "new york": [r"\bNew York\b(?!\s*State)", r"\bNYC\b",
                 r"\bBrooklyn\b(?!\s+(Park|Center))", r"\bManhattan\b"],
    "new york city": [r"\bNew York\b(?!\s*State)", r"\bNYC\b",
                      r"\bBrooklyn\b(?!\s+(Park|Center))", r"\bManhattan\b"],
    "nyc": [r"\bNew York\b(?!\s*State)", r"\bNYC\b",
            r"\bBrooklyn\b(?!\s+(Park|Center))", r"\bManhattan\b"],
    "philadelphia": [r"\bPhiladelphia\b", r"\bPhilly\b"],
    "san francisco": [r"\bSan Francisco\b", r"\bSF\b", r"\bBay Area\b"],
    "washington dc": [r"\bWashington,?\s*D\.?C\b", r"\bD\.C\.", r"\bDC\b"],
    "dc": [r"\bWashington,?\s*D\.?C\b", r"\bD\.C\.", r"\bDC\b"],
    "los angeles": [r"\bLos Angeles\b"],
}


def phrase_to_regex(phrase: str) -> str:
    phrase = phrase.strip()
    if phrase.startswith("re:"):
        return phrase[3:].strip()
    body = re.escape(phrase).replace(r"\*", r"\w*")
    body = re.sub(r"(\\ )+", r"\\s+", body)          # any run of spaces
    return rf"\b{body}\b"


def location_patterns(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        key = re.sub(r"[^a-z ]", "", n.lower()).strip()
        key = re.sub(r"\s+", " ", key)
        for p in LOCATION_ALIASES.get(key, [phrase_to_regex(n)]):
            if p not in out:
                out.append(p)
    return out


def _plain(phrase: str) -> str:
    """A role phrase as plain search words (for keyword APIs like Adzuna)."""
    return "" if phrase.startswith("re:") else phrase.replace("*", "").strip()


# --------------------------------------------------------------------------- #
# Experience presets
# --------------------------------------------------------------------------- #
SENIOR = r"\b(senior|sr\.?|staff|principal|lead|director|head|vp|vice president|chief|distinguished|architect)\b"
STAFF = r"\b(staff|principal)\b"
EXEC = r"\b(director|head of|vp|vice president|chief|distinguished)\b"
LEVELS = [r"\b(II|III|IV|2|3)\s*$", r"\b(manager|pm),?\s+(II|III|IV)\b", r"\bgroup product\b"]
INTERN = r"\b(intern|internship|extern|externship|co-op|coop|summer 20\d\d)\b"
CONTRACT = r"\b(contract|contractor|temporary|part[- ]time)\b"
JUNIOR = r"\b(junior|jr\.?|new grad(uate)?|entry[- ]level|early[- ]career|apprentice)\b"


def _entry_signals() -> list[str]:
    y = date.today().year
    return [
        r"\b(associate|junior|jr\.?|entry|new grad(uate)?|early career|early-career|university|"
        rf"graduate|rotational|apprentice|analyst|class of|{y}|{y + 1})\b",
        r"\bAPM\b",
        r"\b(manager|engineer|consultant|analyst|specialist|developer|scientist),?\s+I\b",
    ]


PRESETS = {
    # label, title exclusions, entry signals, max years, accept unstated, stretch
    "entry": ("Entry-level", [SENIOR, *LEVELS, INTERN, CONTRACT], "signals", 1, False,
              {"enabled": True, "max_years_experience": 2, "max_posting_age_days": 14}),
    "mid": ("Mid-level", [STAFF, EXEC, INTERN, CONTRACT, JUNIOR], [], 5, True,
            {"enabled": True, "max_years_experience": 7, "max_posting_age_days": 14}),
    "senior": ("Senior", [EXEC, INTERN, CONTRACT, JUNIOR], [], 15, True, {"enabled": False}),
    "any": ("All", [INTERN, CONTRACT], [], 99, True, {"enabled": False}),
}

# --------------------------------------------------------------------------- #
# Defaults for everything a person doesn't need to touch
# --------------------------------------------------------------------------- #
DEFAULT_SOURCES = {
    "greenhouse": [
        "affirm", "airbnb", "alphasense", "asana", "betterment", "braze", "brex", "carta",
        "chime", "coinbase", "datadog", "duolingo", "figma", "gusto", "intercom", "lyft",
        "mixpanel", "mongodb", "okta", "pagerduty", "pinterest", "reddit", "robinhood",
        "seatgeek", "sofi", "squarespace", "stripe", "toast", "zocdoc",
    ],
    "lever": ["palantir", "spotify", "wealthfront"],
    "ashby": ["notion", "openai", "perplexity", "ramp", "replit", "sentry", "supabase"],
    "adzuna": {"enabled": False, "max_days_old": 3},
}
DEFAULTS = {
    "locations": [],
    "include_remote": False,
    "roles": {},
    "experience": "entry",
    "exclude_words": [],
    "extra_companies": {"greenhouse": [], "lever": [], "ashby": []},
    "schedule": {"timezone": "America/New_York", "hour": 7, "minute": 0},
    "track_applications": True,
    "send_empty_digest": False,
    "first_run_lookback_days": 21,
    "advanced": {},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def summary(prefs: dict) -> str:
    """One line for the digest header, e.g. 'Entry-level roles in Chicago and remote'."""
    label = PRESETS.get(prefs.get("experience", "entry"), PRESETS["entry"])[0]
    places = [l for l in prefs.get("locations", []) if l.strip().lower() != "remote"]
    if prefs.get("include_remote") or any(l.strip().lower() == "remote" for l in prefs.get("locations", [])):
        places.append("remote")
    if not places:
        return f"{label} roles"
    where = places[0] if len(places) == 1 else ", ".join(places[:-1]) + " and " + places[-1]
    return f"{label} roles in {where}"


def build_config(raw: dict) -> dict:
    if isinstance(raw.get("locations"), dict):          # older all-regex format
        cfg = copy.deepcopy(raw)
        cfg.setdefault("summary", "Matching roles")
        return cfg

    p = _merge(DEFAULTS, raw)
    if any(l.strip().lower() == "remote" for l in p["locations"]):
        p["include_remote"] = True
    level = p["experience"] if p["experience"] in PRESETS else "entry"
    _, exclude, signals, max_years, unstated, stretch = PRESETS[level]

    roles = {group: [phrase_to_regex(x) for x in phrases]
             for group, phrases in p["roles"].items() if phrases}
    if not roles:
        raise ValueError('config.json has no "roles". Run: python -m jobsearch setup')
    locs = [l for l in p["locations"] if l.strip().lower() != "remote"]
    if not locs and not p["include_remote"]:
        raise ValueError('config.json has no "locations". Run: python -m jobsearch setup')

    sources = copy.deepcopy(DEFAULT_SOURCES)
    for kind, slugs in (p["extra_companies"] or {}).items():
        if kind in ("greenhouse", "lever", "ashby"):
            sources[kind] = sorted(set(sources[kind]) | {s.strip().lower() for s in slugs if s.strip()})
    sources["simplify_new_grad"] = level == "entry"
    sources["adzuna"]["queries"] = [q for ps in p["roles"].values() for q in map(_plain, ps) if q]
    sources["adzuna"]["where"] = locs

    cfg = {
        "summary": summary(p),
        "locations": {"include": location_patterns(locs), "include_remote": p["include_remote"]},
        "roles": roles,
        "exclude_title": exclude + [phrase_to_regex(w) for w in p["exclude_words"]],
        "entry_signals": _entry_signals() if signals == "signals" else list(signals),
        "max_years_experience": max_years,
        "include_unstated_experience": unstated,
        "stretch": {"section_name": "Stretch roles (ask for a bit more experience)", **stretch},
        "first_run_lookback_days": p["first_run_lookback_days"],
        "send_empty_digest": p["send_empty_digest"],
        "schedule": p["schedule"],
        "tracker": {"enabled": bool(p["track_applications"]), "scan_days": 4,
                    "show_rejected_days": 14, "aliases": {}},
        "sources": sources,
        "discovery": {"enabled": True, "every_days": 7, "max_probes_per_run": 400,
                      "min_matching_roles": 1, "recheck_days": 90},
    }
    return _merge(cfg, p["advanced"])


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path.name} not found. Run:  python -m jobsearch setup")
    return build_config(json.loads(path.read_text(encoding="utf-8")))
