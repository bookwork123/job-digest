"""Interactive setup wizard:  python -m jobsearch setup

Asks for locations, job titles, experience level and send time, then writes
config.json (keeping any "advanced" or "extra_companies" settings already there)
and points the GitHub Actions schedule at the chosen local time.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import schedule
from .prefs import PRESETS, build_config, summary

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _yes(ask, q: str, default: bool) -> bool:
    a = ask(f"{q} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not a else a.startswith("y")


def run(ask=input, say=print, config: Path = CONFIG, workflow: Path = schedule.WORKFLOW) -> dict:
    old = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    if isinstance(old.get("locations"), dict):
        old = {}                                     # older regex format: start fresh
    say("Job digest setup. Press Enter to keep the value in [brackets].\n")

    # Locations
    cur = ", ".join(old.get("locations", []))
    say("Cities to search, separated by commas (e.g. Chicago, Austin). Add 'Remote' for remote roles.")
    locs = _split(ask(f"Locations [{cur}]: ") or cur)
    while not locs:
        locs = _split(ask("Please enter at least one location: "))

    # Roles
    say("\nJob titles to match, separated by commas. Use * as a wildcard: 'product manag*'")
    say("matches product manager and product management. You can make several groups;")
    say("each group becomes a section of the email.")
    roles: dict[str, list[str]] = {}
    old_roles = old.get("roles") or {}
    if old_roles and _yes(ask, f"Keep your current role groups ({', '.join(old_roles)})?", True):
        roles = dict(old_roles)
    while True:
        name = ask(f"Group name{' (Enter to finish)' if roles else ''}: ").strip()
        if not name:
            if roles:
                break
            continue
        titles = _split(ask(f"  Titles for '{name}': "))
        if titles:
            roles[name] = titles

    # Experience
    say("\nExperience level:")
    keys = list(PRESETS)
    for i, k in enumerate(keys, 1):
        say(f"  {i}. {PRESETS[k][0]}")
    cur_level = old.get("experience", "entry")
    a = ask(f"Choose 1-{len(keys)} [{keys.index(cur_level) + 1 if cur_level in keys else 1}]: ").strip()
    level = keys[int(a) - 1] if a.isdigit() and 1 <= int(a) <= len(keys) else cur_level

    # Exclusions
    cur = ", ".join(old.get("exclude_words", []))
    say("\nWords that should rule a title out, separated by commas (e.g. sales, clearance). Optional.")
    excl = _split(ask(f"Exclude [{cur}]: ") or cur)

    # Schedule
    sched = old.get("schedule") or {"timezone": "America/New_York", "hour": 7, "minute": 0}
    say("\nWhen should the daily email arrive? GitHub usually starts it 5-20 minutes late.")
    while True:
        tz = ask(f"Time zone, e.g. America/Chicago [{sched['timezone']}]: ").strip() or sched["timezone"]
        try:
            schedule.cron_lines(tz, 0)
            break
        except ValueError as e:
            say(f"  {e}")
    hour = ask(f"Hour, 0-23 [{sched['hour']}]: ").strip()
    hour = int(hour) if hour.isdigit() and 0 <= int(hour) <= 23 else int(sched["hour"])
    sched = {"timezone": tz, "hour": hour, "minute": int(sched.get("minute", 0))}

    track = _yes(ask, "\nTrack your applications from Gmail (read-only)?",
                 old.get("track_applications", True))

    new = dict(old)
    new.update(locations=locs, roles=roles, experience=level, exclude_words=excl,
               schedule=sched, track_applications=track)
    new.pop("include_remote", None)                  # "Remote" in locations covers it
    build_config(new)                                # validates
    config.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")

    lines = schedule.cron_lines(tz, hour, sched["minute"])
    try:
        schedule.write_workflow_cron(lines, workflow)
        cron_note = f"GitHub schedule set to {' and '.join(lines)} (UTC)."
    except (OSError, ValueError) as e:
        cron_note = f"Couldn't update the workflow schedule ({e}); set it by hand: {lines}"

    say(f"\nSaved {config.name}: {summary(new)}.")
    say(cron_note)
    say("Next: python -m jobsearch --dry-run --preview preview.html   (shows matches, sends nothing)")
    return new
