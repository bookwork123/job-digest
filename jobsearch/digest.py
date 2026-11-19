"""Builds the HTML digest and sends it over Gmail SMTP."""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from collections import defaultdict
from datetime import datetime
from email.message import EmailMessage

from .sources import Job


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


STATUS_STYLE = {
    "offer": ("#1a7f37", "Offer"),
    "interview": ("#0969da", "Interview"),
    "applied": ("#57606a", "Applied"),
    "rejected": ("#cf222e", "Not moving forward"),
}


def build_applied(apps: list[dict], changes: list[str]) -> str:
    if not apps and not changes:
        return ""
    out = ['<h3 style="border-bottom:1px solid #d0d7de;padding-bottom:4px;margin-top:28px">'
           f'Your applications <span style="color:#57606a;font-weight:normal">({len(apps)})</span></h3>']
    if changes:
        out.append('<p style="margin:0 0 8px;font-size:14px"><b>Updates since last digest:</b><br>'
                   + "<br>".join("• " + _esc(c) for c in changes) + "</p>")
    out.append('<table style="border-collapse:collapse;width:100%;font-size:13px">')
    for a in apps:
        color, label = STATUS_STYLE.get(a["status"], ("#57606a", a["status"].title()))
        applied = a.get("date_applied") or ""
        try:
            applied = datetime.fromisoformat(applied).strftime("%b %d")
        except ValueError:
            pass
        title = _esc(a["company"]) + (f" &middot; {_esc(a['role'])}" if a.get("role") else "")
        if a.get("url"):
            title = f'<a href="{_esc(a["url"])}" style="color:#1f2328;text-decoration:none">{title}</a>'
        out.append(f'<tr><td style="padding:4px 0;border-bottom:1px solid #eaeef2">{title}</td>'
                   f'<td style="padding:4px 8px;border-bottom:1px solid #eaeef2;white-space:nowrap;color:{color};font-weight:600">{label}</td>'
                   f'<td style="padding:4px 0;border-bottom:1px solid #eaeef2;white-space:nowrap;color:#8c959f">{_esc(applied)}</td></tr>')
    out.append("</table>")
    return "\n".join(out)


def build_html(jobs: list[Job], errors: list[str], category_order: list[str],
               first_run: bool, apps: list[dict] | None = None,
               app_changes: list[str] | None = None,
               new_companies: list[str] | None = None, summary: str = "") -> str:
    by_cat: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        by_cat[j.category].append(j)

    today = datetime.now().strftime("%A, %B %d")
    intro = ("First run: here's what's currently open that fits."
             if first_run else "New postings since the last digest.")
    parts = [f"""<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:720px;color:#1f2328">
<h2 style="margin:0 0 4px">Job digest &middot; {today}</h2>
<p style="margin:0 0 16px;color:#57606a">{len(jobs)} match{'es' if len(jobs) != 1 else ''}. {intro}
{_esc(summary) + "." if summary else ""}</p>"""]

    for cat in category_order:
        group = by_cat.get(cat)
        if not group:
            continue
        group.sort(key=lambda j: (j.posted is None, -(j.posted.timestamp() if j.posted else 0)))
        parts.append(f'<h3 style="border-bottom:1px solid #d0d7de;padding-bottom:4px">'
                     f'{_esc(cat)} <span style="color:#57606a;font-weight:normal">({len(group)})</span></h3>')
        parts.append('<table style="border-collapse:collapse;width:100%;font-size:14px">')
        for j in group:
            posted = j.posted.strftime("%b %d") if j.posted else ""
            locs = "; ".join(j.locations[:3]) + (" …" if len(j.locations) > 3 else "")
            tags = " · ".join(j.tags)
            parts.append(f"""<tr><td style="padding:6px 0;border-bottom:1px solid #eaeef2">
<a href="{_esc(j.url)}" style="font-weight:600;color:#0969da;text-decoration:none">{_esc(j.title)}</a><br>
<span>{_esc(j.company)}</span> &middot; <span style="color:#57606a">{_esc(locs)}</span><br>
<span style="color:#8c959f;font-size:12px">{_esc(j.source)}{' · posted ' + posted if posted else ''}{' · ' + _esc(tags) if tags else ''}</span>
</td></tr>""")
        parts.append("</table>")

    if not jobs:
        parts.append("<p>No new job matches today.</p>")
    parts.append(build_applied(apps or [], app_changes or []))
    if new_companies:
        names = sorted(new_companies, key=str.lower)
        parts.append('<p style="font-size:13px;color:#57606a;margin-top:20px"><b>Now following '
                     f'{len(names)} new compan{"ies" if len(names) != 1 else "y"}</b> '
                     '(found this week with matching roles in your locations): '
                     + _esc(", ".join(names[:40])) + (" …" if len(names) > 40 else "") + "</p>")
    if errors:
        parts.append('<p style="color:#8c959f;font-size:12px;margin-top:24px">'
                     f'{len(errors)} source error(s): ' + _esc("; ".join(errors[:10]))
                     + (" …" if len(errors) > 10 else "") + "</p>")
    parts.append("</div>")
    return "\n".join(parts)


def build_text(jobs: list[Job]) -> str:
    lines = [f"{len(jobs)} new matching job(s):", ""]
    for j in jobs:
        lines.append(f"- {j.title} | {j.company} | {'; '.join(j.locations[:2])}\n  {j.url}")
    return "\n".join(lines)


def send(subject: str, html_body: str, text_body: str) -> str:
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("DIGEST_TO") or sender
    if not (sender and password):
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Job Digest <{sender}>", to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, password.replace(" ", ""))
        s.send_message(msg)
    return to
