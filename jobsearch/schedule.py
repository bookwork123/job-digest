"""Daily send time: local time in config.json <-> GitHub Actions cron (UTC).

GitHub cron only understands UTC, and most time zones shift by an hour for
daylight saving. So the workflow has two cron lines (the UTC times for the
chosen local time in winter and in summer), and a gate step lets through only
the one that lands on the configured local time today:

    python -m jobsearch.schedule gate <event_name> "<cron that fired>"
    -> prints go=true / go=false (appended to $GITHUB_OUTPUT)

`python -m jobsearch setup` rewrites the cron lines with cron_lines().
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "daily-digest.yml"

# Fallback when the OS has no time-zone database (e.g. Python on Windows):
# (standard UTC offset in hours, observes daylight saving)
COMMON_ZONES = {
    "America/New_York": (-5, True), "America/Chicago": (-6, True),
    "America/Denver": (-7, True), "America/Phoenix": (-7, False),
    "America/Los_Angeles": (-8, True), "America/Anchorage": (-9, True),
    "Pacific/Honolulu": (-10, False), "America/Toronto": (-5, True),
    "America/Vancouver": (-8, True), "Europe/London": (0, True),
    "Europe/Dublin": (0, True), "Europe/Paris": (1, True), "Europe/Berlin": (1, True),
    "Europe/Amsterdam": (1, True), "Asia/Kolkata": (5.5, False),
    "Asia/Singapore": (8, False), "Asia/Tokyo": (9, False), "Australia/Sydney": (10, True),
    "UTC": (0, False),
}


def _offsets(tz: str) -> list[timedelta]:
    """UTC offsets the zone uses during the year (one, or two with DST)."""
    try:
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz)
        year = datetime.now().year
        offs = {datetime(year, m, 15, 12, tzinfo=z).utcoffset() for m in (1, 7)}
        return sorted(offs)
    except Exception:
        if tz not in COMMON_ZONES:
            raise ValueError(f"unknown time zone {tz!r} (use a name like America/Chicago)")
        std, dst = COMMON_ZONES[tz]
        offs = {timedelta(hours=std)}
        if dst:
            offs.add(timedelta(hours=std + 1))
        return sorted(offs)


def cron_lines(tz: str, hour: int, minute: int = 0) -> list[str]:
    out = []
    for off in _offsets(tz):
        utc = (datetime(2000, 1, 2, hour, minute) - off)
        line = f"{utc.minute} {utc.hour} * * *"
        if line not in out:
            out.append(line)
    return out


def write_workflow_cron(lines: list[str], path: Path = WORKFLOW) -> None:
    text = path.read_text(encoding="utf-8")
    block = "\n".join(f'    - cron: "{l}"' for l in lines)
    new, n = re.subn(r"(# BEGIN SCHEDULE[^\n]*\n).*?(\n\s*# END SCHEDULE)",
                     lambda m: m.group(1) + block + m.group(2), text, flags=re.S)
    if n != 1:
        raise ValueError(f"schedule markers not found in {path}")
    path.write_text(new, encoding="utf-8")


def should_run(event: str, fired: str, sched: dict, now: datetime | None = None) -> bool:
    """True unless this is a scheduled run whose cron isn't today's local slot."""
    if event != "schedule":
        return True
    m = re.match(r"\s*(\d+)\s+(\d+)\s", fired or "")
    if not m:
        return True
    from zoneinfo import ZoneInfo          # GitHub's Linux runners have tzdata
    now = now or datetime.now(timezone.utc)
    slot = now.replace(hour=int(m.group(2)), minute=int(m.group(1)), second=0, microsecond=0)
    if slot > now + timedelta(hours=1):    # a late start past UTC midnight
        slot -= timedelta(days=1)
    local = slot.astimezone(ZoneInfo(sched.get("timezone", "UTC")))
    return (local.hour, local.minute) == (int(sched.get("hour", 7)), int(sched.get("minute", 0)))


def _main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "gate":
        event = argv[1] if len(argv) > 1 else ""
        fired = argv[2] if len(argv) > 2 else ""
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        go = should_run(event, fired, cfg.get("schedule") or {})
        print(f"go={'true' if go else 'false'}")
        print(f"cron '{fired}' -> go={go}", file=sys.stderr)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
