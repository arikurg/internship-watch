#!/usr/bin/env python3
"""Email me when Simplify's internship feed lists a new posting that matches config.json.

Runs on a schedule in GitHub Actions. Standard library only, so the workflow
needs no pip install step.

Usage:
  python3 watch.py                 normal run: email new matches, update state
  python3 watch.py --dry-run       print the email instead of sending; state untouched
  python3 watch.py --test-email    email every matching open role; state untouched
  python3 watch.py --feed-file F   read listings from a local file (for testing)

Email settings come from environment variables:
  SMTP_USER, SMTP_PASSWORD   login (for Gmail: your address and an app password)
  EMAIL_TO                   recipient, or several separated by commas
  EMAIL_FROM                 optional, defaults to SMTP_USER
  SMTP_HOST, SMTP_PORT       optional, default smtp.gmail.com:465
  SMTP_STARTTLS              optional, set to 0 to skip STARTTLS on non-465 ports
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"

# GitHub pauses scheduled workflows in public repos after 60 days with no
# commits. If nothing new has shown up for this long, we touch the state file
# so the workflow commits it and the schedule stays alive.
HEARTBEAT_DAYS = 45


# --------------------------------------------------------------------------- #
# Feed
# --------------------------------------------------------------------------- #

def fetch_listings(url: str, attempts: int = 3) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "internship-watch"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            if not isinstance(data, list) or not data:
                raise ValueError(f"feed returned {type(data).__name__} with no listings")
            return data
        except Exception as exc:  # network errors, bad JSON, empty feed
            last_error = exc
            print(f"fetch attempt {attempt} failed: {exc}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"could not fetch feed after {attempts} attempts: {last_error}")


def load_listings(args, config) -> list[dict]:
    if args.feed_file:
        return json.loads(Path(args.feed_file).read_text())
    return fetch_listings(config["feed_url"])


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def word_regex(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    alternatives = "|".join(re.escape(w.strip()) for w in words if w.strip())
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


class Watch:
    """One entry from config["watches"].

    A listing matches when its company is in scope and either its Simplify
    category is in `categories` or its title contains one of `title_keywords`.
    `title_exclude` and `degrees_any` fall back to the top-level config values.
    """

    def __init__(self, spec: dict, config: dict):
        self.name = spec["name"]
        companies = spec.get("companies", "any")
        self.companies = None if companies in ("any", "*", None) else {c.lower() for c in companies}
        self.categories = {c.lower() for c in spec.get("categories", [])}
        self.include = word_regex(spec.get("title_keywords", []))
        self.exclude = word_regex(spec.get("title_exclude", config.get("title_exclude", [])))
        self.degrees = {d.lower() for d in spec.get("degrees_any", config.get("degrees_any", []))}

    def matches(self, listing: dict) -> bool:
        if self.companies is not None and listing.get("company_name", "").lower() not in self.companies:
            return False
        # Postings with no degree info pass; otherwise one listed degree must be yours.
        degrees = {d.lower() for d in listing.get("degrees") or []}
        if self.degrees and degrees and not degrees & self.degrees:
            return False
        title = listing.get("title", "")
        if self.exclude and self.exclude.search(title):
            return False
        if listing.get("category", "").lower() in self.categories:
            return True
        return bool(self.include and self.include.search(title))


class Matcher:
    """Returns the name of the first watch a listing matches, or None."""

    def __init__(self, config: dict):
        self.watches = [Watch(spec, config) for spec in config["watches"]]
        self.skip_terms = {t.lower() for t in config.get("skip_terms", [])}

    def __call__(self, listing: dict) -> str | None:
        if not listing.get("active") or not listing.get("is_visible", True):
            return None
        # Skip roles whose every listed term has already started or passed.
        terms = {t.lower() for t in listing.get("terms") or []}
        if self.skip_terms and terms and terms <= self.skip_terms:
            return None
        for watch in self.watches:
            if watch.matches(listing):
                return watch.name
        return None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text())


def save_state(seen: set[str], last_change: datetime) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_change": last_change.isoformat(timespec="seconds"), "seen": sorted(seen)}
    STATE_PATH.write_text(json.dumps(payload, indent=1) + "\n")


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

def local_tz(config: dict):
    name = config.get("timezone") or "UTC"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def describe_age(posted: datetime, now: datetime) -> str:
    minutes = int((now - posted).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (60 * 24)} days ago"


def posting_fields(p: dict, tz, now: datetime) -> dict:
    posted = datetime.fromtimestamp(p.get("date_posted", 0), tz=timezone.utc)
    stamp = posted.astimezone(tz).strftime("%b %d, %I:%M %p %Z").replace(" 0", " ")
    return {
        "title": p.get("title", "(untitled)"),
        "company": p.get("company_name", ""),
        "url": p.get("url", ""),
        "locations": ", ".join(p.get("locations") or []) or "Location not listed",
        "terms": ", ".join(p.get("terms") or []) or "Term not listed",
        "posted": f"{stamp} ({describe_age(posted, now)})",
        "category": p.get("category", ""),
        "watch": p.get("_watch", ""),
    }


def company_label(companies: list[str], limit: int = 3) -> str:
    if len(companies) <= limit:
        return ", ".join(companies)
    return f"{', '.join(companies[:limit])} +{len(companies) - limit} more"


def build_email(postings: list[dict], config: dict, heading: str) -> tuple[str, str, str]:
    tz = local_tz(config)
    now = datetime.now(timezone.utc)
    rows = [posting_fields(p, tz, now) for p in postings]

    # Most frequent companies first, so the subject names the bulk of the batch.
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["company"]] = counts.get(r["company"], 0) + 1
    companies = sorted(counts, key=lambda c: (-counts[c], c))

    if heading == "new" and len(rows) == 1:
        subject = f"New {rows[0]['company']} internship: {rows[0]['title']}"
    elif heading == "new" and len(companies) == 1:
        subject = f"{len(rows)} new {companies[0]} internships"
    elif heading == "new":
        subject = f"{len(rows)} new internships ({company_label(companies)})"
    elif heading == "live":
        subject = f"Internship watcher is live: {len(rows)} open roles"
    else:
        subject = f"[test] {len(rows)} open roles match your filters"

    intro = {
        "new": "New postings that match your filters:",
        "live": "Your watcher is running. These matching roles are open right now, "
                "and you'll get an email when a new one appears.",
        "test": "Test email. These matching roles are open right now:",
    }[heading]
    if not rows:
        intro += "\n\n(No open roles match your filters at the moment.)"

    # Group by watch, in the order the watches appear in config.json.
    order = [w["name"] for w in config["watches"]]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["watch"], []).append(r)
    sections = sorted(groups.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))

    text_lines = [intro, ""]
    html_parts = []
    for name, group in sections:
        text_lines += [f"== {name} ({len(group)}) ==", ""]
        html_parts.append(
            f'<tr><td style="padding:20px 0 4px;font-size:13px;font-weight:700;letter-spacing:.04em;'
            f'text-transform:uppercase;color:#555">{escape(name)} ({len(group)})</td></tr>'
        )
        for r in group:
            text_lines += [
                r["title"],
                f"  {r['company']} | {r['locations']} | {r['terms']}",
                f"  Posted {r['posted']} | {r['category']}",
                f"  Apply: {r['url']}",
                "",
            ]
            html_parts.append(f"""
        <tr><td style="padding:12px 0;border-bottom:1px solid #e5e5e5">
          <a href="{escape(r['url'])}" style="font-size:16px;font-weight:600;color:#1a56db;text-decoration:none">{escape(r['title'])}</a>
          <div style="color:#333;margin-top:4px"><b>{escape(r['company'])}</b> &middot; {escape(r['locations'])} &middot; {escape(r['terms'])}</div>
          <div style="color:#777;font-size:13px;margin-top:2px">Posted {escape(r['posted'])} &middot; {escape(r['category'])}</div>
        </td></tr>""")
    text_lines.append("Source: SimplifyJobs internship feed")
    text = "\n".join(text_lines)

    intro_html = escape(intro).replace("\n", "<br>")
    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:620px;margin:0 auto;padding:16px;color:#111">
  <p style="margin:0 0 8px">{intro_html}</p>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{''.join(html_parts)}</table>
  <p style="color:#999;font-size:12px;margin-top:16px">Source: SimplifyJobs internship feed</p>
</body></html>"""
    return subject, text, html


def send_email(subject: str, text: str, html: str) -> None:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    to = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    sender = os.environ.get("EMAIL_FROM") or user or (to[0] if to else "")
    if not to:
        raise RuntimeError("EMAIL_TO is not set")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        if os.environ.get("SMTP_STARTTLS", "1") != "0":
            server.starttls(context=context)
    with server:
        if user and password:
            server.login(user, password)
        server.send_message(msg)


def deliver(postings, config, heading, dry_run) -> None:
    subject, text, html = build_email(postings, config, heading)
    if dry_run:
        print(f"--- dry run: would send ---\nSubject: {subject}\n\n{text}\n---")
        return
    send_email(subject, text, html)
    print(f"sent: {subject}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--feed-file")
    args = parser.parse_args()
    if os.environ.get("TEST_EMAIL") in {"1", "true"}:
        args.test_email = True

    config = json.loads(CONFIG_PATH.read_text())
    listings = load_listings(args, config)
    matcher = Matcher(config)
    open_roles = []
    for listing in listings:
        watch_name = matcher(listing)
        if watch_name:
            listing["_watch"] = watch_name
            open_roles.append(listing)
    open_roles.sort(key=lambda l: l.get("date_posted", 0), reverse=True)
    print(f"feed has {len(listings)} listings; {len(open_roles)} open roles match")
    for watch in matcher.watches:
        print(f"  {watch.name}: {sum(1 for l in open_roles if l['_watch'] == watch.name)}")

    if args.test_email:
        deliver(open_roles, config, "test", args.dry_run)
        return 0

    now = datetime.now(timezone.utc)
    state = load_state()

    if state is None:
        print("first run: recording current roles as seen")
        if config.get("notify_on_first_run", True):
            deliver(open_roles, config, "live", args.dry_run)
        if not args.dry_run:
            save_state({l["id"] for l in open_roles}, now)
        return 0

    seen = set(state.get("seen", []))
    new = [l for l in open_roles if l["id"] not in seen]
    print(f"{len(new)} new")

    if new:
        # Send first. If the email fails, the run fails and the state stays
        # unchanged, so the next run retries the same postings.
        deliver(new, config, "new", args.dry_run)
        if not args.dry_run:
            save_state(seen | {l["id"] for l in new}, now)
        return 0

    last_change = datetime.fromisoformat(state.get("last_change", now.isoformat()))
    if now - last_change > timedelta(days=HEARTBEAT_DAYS) and not args.dry_run:
        print("no changes for a while: refreshing state to keep the schedule alive")
        save_state(seen, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
