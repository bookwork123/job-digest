"""Entry point.

    python -m jobsearch setup                 # answer a few questions, writes config.json
    python -m jobsearch [--dry-run] [--preview FILE] [--reset] ...
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import digest
from .applications import Tracker, scan_gmail
from .discover import Discovery
from .filters import Matcher
from .prefs import load_config
from .sources import fetch_all
from .store import SeenStore

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["setup"]:
        from .wizard import run
        run()
        return 0
    ap = argparse.ArgumentParser(description="Daily job-posting digest")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't send email or update seen-store")
    ap.add_argument("--preview", metavar="FILE",
                    help="also write the digest HTML to FILE")
    ap.add_argument("--reset", action="store_true",
                    help="forget everything already sent (next run acts like the first)")
    ap.add_argument("--scan-days", type=int,
                    help="how many days of Gmail to scan for application emails "
                         "(default: tracker.scan_days in config)")
    ap.add_argument("--discover", action="store_true",
                    help="run company discovery now instead of waiting for the weekly slot")
    ap.add_argument("--send-empty", action="store_true",
                    help="send the email even if nothing is new (to test email setup)")
    args = ap.parse_args(argv)

    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(ROOT / "logs" / "run.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    log = logging.getLogger("jobsearch")

    load_env(ROOT / ".env")
    try:
        cfg = load_config(ROOT / "config.json")
    except ValueError as e:
        log.error("%s", e)
        return 2
    store_path = ROOT / "data" / "seen.json"
    # --reset starts from an empty store; the file is only overwritten on a
    # successful real run, so `--reset --dry-run` is safe.
    store = SeenStore(store_path, fresh=args.reset)

    # Weekly: look for new companies to follow, then merge them into the sources.
    discovery = Discovery(ROOT / "data" / "companies.json", cfg)
    if args.discover or discovery.due():
        known = {k: set(cfg["sources"].get(k) or []) for k in ("greenhouse", "lever", "ashby")}
        try:
            discovery.run(Matcher(cfg), known, log)
        except Exception as e:
            log.warning("discovery failed: %s", e)
    for kind, slugs in discovery.slugs().items():
        cfg["sources"][kind] = sorted(set(cfg["sources"].get(kind) or []) | set(slugs))
    new_companies = discovery.added_now

    jobs, errors = fetch_all(cfg)
    log.info("fetched %d postings (%d source errors)", len(jobs), len(errors))
    for e in errors:
        log.warning("source error: %s", e)

    matched = Matcher(cfg).run(jobs)

    # Collapse cross-source duplicates (same company + title).
    uniq: dict[str, object] = {}
    for j in matched:
        uniq.setdefault(j.fuzzy_key, j)
    matched = list(uniq.values())

    new = [j for j in matched if store.is_new(j)]
    if store.is_first_run:
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.get("first_run_lookback_days", 21))
        new = [j for j in new if j.posted is None or j.posted >= cutoff]
    elif new_companies:
        # A just-added company's whole backlog is "unseen"; only show its recent posts.
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.get("first_run_lookback_days", 21))
        fresh = set(new_companies)
        new = [j for j in new if j.company not in fresh or j.posted is None or j.posted >= cutoff]
    log.info("%d matched, %d new", len(matched), len(new))

    # Application tracker: pick up confirmations / interviews / rejections from Gmail.
    tcfg = cfg.get("tracker") or {}
    tracker = None
    if tcfg.get("enabled"):
        tracker = Tracker(ROOT / "data" / "applications.csv", tcfg.get("aliases"))
        user, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
        if user and pw:
            try:
                n = scan_gmail(tracker, user, pw, args.scan_days or tcfg.get("scan_days", 4), log)
                log.info("tracker: %d application emails read, %d change(s)", n, len(tracker.changes))
                for c in tracker.changes:
                    log.info("  tracker: %s", c)
            except Exception as e:
                errors.append(f"gmail scan: {e}")
                log.warning("gmail scan failed: %s", e)
    apps = tracker.for_digest(tcfg.get("show_rejected_days", 14)) if tracker else []
    app_changes = tracker.changes if tracker else []

    sections = list(cfg["roles"])
    if (cfg.get("stretch") or {}).get("enabled"):
        sections.append(cfg["stretch"].get("section_name", "Stretch roles"))
    html_body = digest.build_html(new, errors, sections, store.is_first_run,
                                  apps=apps, app_changes=app_changes,
                                  new_companies=new_companies,
                                  summary=cfg.get("summary", ""))
    if args.preview:
        Path(args.preview).write_text(html_body, encoding="utf-8")
        log.info("preview written to %s", args.preview)

    if args.dry_run:
        for j in new:
            log.info("  [%s] %s | %s | %s", j.category, j.title, j.company, "; ".join(j.locations[:2]))
        return 0

    if new or app_changes or new_companies or args.send_empty or cfg.get("send_empty_digest"):
        subject = f"Job digest: {len(new)} new match{'es' if len(new) != 1 else ''}"
        if app_changes:
            subject += f", {len(app_changes)} application update{'s' if len(app_changes) != 1 else ''}"
        try:
            to = digest.send(subject, html_body, digest.build_text(new))
            log.info("emailed digest to %s", to)
        except Exception as e:
            log.error("email failed, nothing marked as seen (will retry next run): %s", e)
            return 1
    else:
        log.info("nothing new; no email sent")

    store.mark(matched)  # new ones + refresh still-open ones
    store.save()
    if tracker:
        tracker.save()
    discovery.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
