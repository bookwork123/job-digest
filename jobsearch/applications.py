"""Application tracker (data/applications.csv), updated from Gmail.

Each run scans recent mail for application confirmations, interview requests
and status updates, and adds or updates rows. Only headers and the first few
KB of the plain-text body of candidate messages are read; nothing from the
email is stored except company, role, status and the date.
"""
from __future__ import annotations

import csv
import html
import email
import imaplib
import re
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

FIELDS = ["date_applied", "company", "role", "status", "last_update", "source", "url", "notes"]
STATUS_RANK = {"drafted": 0, "applied": 1, "interview": 2, "offer": 3}
ACTIVE = {"drafted", "applied", "interview", "offer"}

SKIP_SUBJECT = re.compile(
    r"apply now|love to see you apply|security code|share your feedback|verify your|"
    r"job alert|jobs? (?:for|in) .* for you|recommended jobs|new jobs|voter|^(?:re|fwd?):", re.I)
CONFIRM = re.compile(
    r"thank(?:s| you)\b[\w\s,!]*\bfor (?:applying|your application|submitting)|"
    r"we(?:'ve| have)? received your|your application (?:for|to|at|has been)|"
    r"application (?:received|submitted|confirmation)|^your .+ application$|"
    r"application has been (?:submitted|received)|^confirming your|"
    r"^application for\b", re.I)
INTERVIEW = re.compile(
    r"\binterview\b|schedule (?:a|an|your) (?:call|time|chat|conversation)|"
    r"next steps (?:in|for|with) your (?:application|candidacy)|your availability|phone screen", re.I)
UPDATE = re.compile(
    r"update on your|regarding your application|application (?:status|update)|"
    r"status of your application", re.I)
REJECT_BODY = re.compile(
    r"unfortunately|not (?:to )?(?:move|moving) forward|decided to (?:move forward|pursue|proceed) "
    r"with other|no longer (?:under consideration|being considered)|position has been filled|"
    r"will not be (?:moving|proceeding)|not selected|"
    r"(?:selected|chose|chosen|choose) (?:another|other|a different) candidates?|"
    r"(?:move|moving|proceed|proceeding) forward with (?:another|other) candidates?|"
    r"pursue other candidates|decided not to (?:move|proceed|advance)|"
    r"won't be (?:moving|proceeding)|filled (?:the|this) (?:position|role)", re.I)
SKIP_SENDER = re.compile(
    r"jobalerts-noreply|messages-noreply@linkedin|notifications-noreply@linkedin|@em\.linkedin|"
    r"glassdoor|booking\.com|"
    r"ziprecruiter|@indeed\.com|alerts?@|newsletter|marketing", re.I)
ATS_SENDER = re.compile(
    r"greenhouse-mail\.io|hire\.lever\.co|ashbyhq\.com|myworkday\.com|icims\.com|"
    r"smartrecruiters\.com|paradox\.ai|brighthire\.ai|jobvite|taleo|successfactors|"
    r"indeedapply|jobs-noreply@linkedin\.com", re.I)
JOB_SUBJECT = re.compile(
    r"\b(?:application|applying|applied|interview|candidacy|position|role|recruit\w*|"
    r"hiring|job)\b", re.I)
OFFER_BODY = re.compile(r"pleased to (?:extend|offer)|offer letter|offer of employment", re.I)

COMPANY_FROM_SUBJECT = [
    re.compile(r"(?:applying|application|apply|applied) (?:to|at|with) (?:the )?(.+?)(?:!|\.|$|\s+-\s+|\s+for\s)", re.I),
    re.compile(r"^(?:your|we(?:'ve| have)? received your) (.+?) (?:fellowship |internship |job )?application$", re.I),
    re.compile(r"interview at (.+?) for", re.I),
    re.compile(r"interview with (?!you\b)(.+?)$", re.I),
    re.compile(r"^confirming your (.+?) (?:job )?application\b", re.I),
    re.compile(r"^(.+?) job application\b", re.I),
    re.compile(r"^(.+?) would like to schedule", re.I),
]
ROLE_FROM_SUBJECT = [
    re.compile(r"your (.+?) application (?:at|to|with)", re.I),
    re.compile(r"application for (?:the )?(.+?)(?:\s*\(REF\w+\)|\s+REF\w+|\s+at\s|$)", re.I),
    re.compile(r"interview\S*\s+(?:at .+? )?for\s*(.+?)(?:\s*\(REF\w+\)|\s+is\s+confirmed|$)", re.I),
    re.compile(r"video interview (.+?)\s*REF\w+", re.I),
]
ROLE_FROM_BODY = [
    re.compile(r"(?:applying|application|applied|interest) (?:for|to|in) (?:the |our )?([A-Z][^\n.,!]{3,80}?) "
               r"(?:position|role|opening|job)\b"),
]
SENDER_NOISE = re.compile(
    r"\b(?:hiring team|recruiting|recruitment|talent(?: acquisition)?|careers?|jobs|myhr|hr|team|"
    r"no-?reply|notifications?)\b|via .*$", re.I)
GENERIC_SENDERS = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "icims",
                   "brighthire", "myhr", ""}
# ATS domains where the mailbox name is the company (e.g. acme@myworkday.com)
LOCALPART_DOMAINS = ("myworkday.com", "myworkdayjobs.com")


def _h(msg, key: str) -> str:
    raw = msg.get(key, "")
    try:
        raw = str(make_header(decode_header(raw)))
    except Exception:
        pass
    return " ".join(raw.replace("​", "").split())


def _clean(s: str) -> str:
    s = re.sub(r"[​‌‍﻿]", "", s or "")
    s = re.sub(r"\s+", " ", s).strip(" -–:!.,\"'")
    s = re.sub(r"^(?:the )?position:?\s+", "", s, flags=re.I)
    return re.sub(r"\s+(?:position|role)$", "", s, flags=re.I)


def _key(company: str) -> str:
    words = re.findall(r"[a-z0-9]+", company.lower())
    return words[0] if words else ""


def _norm_role(role: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", role.lower()))


def _body_text(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            return (part.get_payload(decode=True) or b"").decode(errors="ignore")[:4000]
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            t = (part.get_payload(decode=True) or b"").decode(errors="ignore")
            # Drop <style>/<script>/<head> first: HTML emails often start with
            # several KB of CSS that would otherwise crowd out the real text.
            t = re.sub(r"<(style|script|head)\b.*?</\1\s*>", " ", t, flags=re.S | re.I)
            t = html.unescape(re.sub(r"<[^>]+>", " ", t))
            return " ".join(t.split())[:6000]
    return ""


class Tracker:
    def __init__(self, path: Path, aliases: dict[str, str] | None = None):
        self.path = path
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self.rows: list[dict] = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as f:
                self.rows = [{k: r.get(k, "") for k in FIELDS} for r in csv.DictReader(f)]
        self.changes: list[str] = []

    def canonical(self, company: str) -> str:
        c = _clean(company)
        for k, v in self.aliases.items():
            if k in c.lower():
                return v
        return c

    def find(self, company: str, role: str) -> dict | None:
        k = _key(company)
        cands = [r for r in self.rows if _key(r["company"]) == k]
        if not cands:
            return None
        nr = _norm_role(role)
        if nr:
            for r in cands:
                rr = _norm_role(r["role"])
                if rr and (rr == nr or rr in nr or nr in rr):
                    return r
            blank = [r for r in cands if not r["role"]]
            return blank[0] if blank else None
        return cands[0] if len(cands) == 1 else None

    def record(self, company: str, role: str, status: str, when: date, source: str,
               url: str = "", note: str = "", allow_new: bool = True) -> None:
        company = self.canonical(company)
        role = _clean(role)
        row = self.find(company, role)
        d = when.isoformat()
        if row is None:
            if not allow_new:
                return
            self.rows.append({"date_applied": d if status != "drafted" else "", "company": company,
                              "role": role, "status": status, "last_update": d,
                              "source": source, "url": url, "notes": note})
            self.changes.append(f"{company}: {role or 'application'} added ({status})")
            return
        if role and not row["role"]:
            row["role"] = role
        if not row["date_applied"] and status in STATUS_RANK and STATUS_RANK[status] >= 1:
            row["date_applied"] = d
        old = row["status"]
        advance = (status == "rejected" and old != "rejected") or (
            status in STATUS_RANK and old in STATUS_RANK and STATUS_RANK[status] > STATUS_RANK[old])
        if advance and d >= (row["last_update"] or ""):
            row["status"], row["last_update"] = status, d
            if note:
                row["notes"] = note
            self.changes.append(f"{row['company']}: {row['role'] or 'application'} is now {status}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows.sort(key=lambda r: (r["date_applied"] or r["last_update"]), reverse=True)
        with self.path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(self.rows)

    def for_digest(self, rejected_days: int = 14) -> list[dict]:
        cutoff = (date.today() - timedelta(days=rejected_days)).isoformat()
        rows = [r for r in self.rows if r["status"] in ACTIVE - {"drafted"}
                or (r["status"] == "rejected" and r["last_update"] >= cutoff)]
        order = {"offer": 0, "interview": 1, "applied": 2, "rejected": 3}
        rows.sort(key=lambda r: r["date_applied"] or r["last_update"], reverse=True)  # newest first
        return sorted(rows, key=lambda r: order.get(r["status"], 9))                  # stable by status


def classify(subject: str, body: str) -> str | None:
    if SKIP_SUBJECT.search(subject):
        return None
    if OFFER_BODY.search(body) and not CONFIRM.search(subject):
        return "offer"
    if REJECT_BODY.search(body):
        return "rejected"
    if INTERVIEW.search(subject):
        return "interview"
    if CONFIRM.search(subject):
        return "applied"
    if UPDATE.search(subject):
        return "update"
    return None


def extract(subject: str, sender: str, body: str) -> tuple[str, str]:
    company = role = ""
    for p in COMPANY_FROM_SUBJECT:
        m = p.search(subject)
        if m:
            company = _clean(m.group(1))
            break
    if not company:
        name, addr = parseaddr(sender)
        local, _, domain = addr.lower().partition("@")
        if domain.endswith(LOCALPART_DOMAINS) and local not in GENERIC_SENDERS:
            name = local.upper() if len(local) <= 3 else local.title()
        name = re.sub(r"^.+? from ", "", name)
        name = _clean(SENDER_NOISE.sub("", name))
        if name.lower() in GENERIC_SENDERS:
            dom = addr.split("@")[-1].split(".")
            name = dom[-2] if len(dom) >= 2 else ""
            name = "" if name.lower() in GENERIC_SENDERS | {"gmail", "greenhouse-mail"} else name
        company = name
    for p in ROLE_FROM_SUBJECT:
        m = p.search(subject)
        if m:
            role = _clean(m.group(1))
            break
    if not role:
        for p in ROLE_FROM_BODY:
            m = p.search(body)
            if m:
                role = _clean(m.group(1))
                break
    if company and role.lower().startswith(company.lower()):
        role = ""  # e.g. "Acme Fellowship" parsed as role
    return company, role


def scan_gmail(tracker: Tracker, user: str, password: str, days: int, log) -> int:
    """Scan the last `days` days of mail. Returns number of messages used."""
    # Also look at any mail naming a company with an open application, so status
    # emails with unusual subjects ("Next steps", "Your candidacy") aren't missed.
    active = sorted({r["company"] for r in tracker.rows if r["status"] in ACTIVE and r["company"]})
    named = " OR ".join(f'"{c}"' for c in active)
    since_q = (f'newer_than:{days}d -from:me -category:promotions (subject:(applying OR application OR '
               f'applied OR interview OR candidacy OR "thank you for your interest" OR '
               f'"your application was sent") OR from:(greenhouse-mail.io OR hire.lever.co OR '
               f'ashbyhq.com OR myworkday.com OR icims.com OR smartrecruiters.com OR paradox.ai)'
               + (f" OR {named}" if named else "") + ")")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, password.replace(" ", ""))
    try:
        M.select('"[Gmail]/All Mail"', readonly=True)
        quoted = since_q.replace("\\", "\\\\").replace('"', '\\"')
        typ, data = M.search(None, f'X-GM-RAW "{quoted}"')
        ids = data[0].split() if typ == "OK" else []
        used = 0
        for i in ids:
            typ, d = M.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            msg = email.message_from_bytes(d[0][1])
            subject, sender = _h(msg, "Subject"), _h(msg, "From")
            if SKIP_SUBJECT.search(subject) or SKIP_SENDER.search(sender):
                continue
            body = _body_text(msg)
            status = classify(subject, body)
            if not status:
                continue
            company, role = extract(subject, sender, body)
            if not company:
                log.info("application email skipped (no company): %s", subject)
                continue
            try:
                when = parsedate_to_datetime(msg.get("Date")).date()
            except Exception:
                when = date.today()
            if status == "update":
                continue  # neutral update with no clear outcome
            # Only clearly job-related mail may create a new row; looser matches
            # (found because they name a tracked company) can only update one.
            strong = bool(ATS_SENDER.search(sender) or JOB_SUBJECT.search(subject))
            tracker.record(company, role, status, when, source="email", allow_new=strong)
            used += 1
        return used
    finally:
        M.logout()
