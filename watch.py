#!/usr/bin/env python3
"""Email me when a new internship that matches config.json shows up.

Reads Simplify's internship feed, plus companies' own job sites listed under
direct_sources in config.json (Greenhouse, Ashby and Lever job boards, and Tesla
through the Chrome extension's Gist), so roles Simplify hasn't picked up still
reach you. Runs on a schedule in GitHub Actions. Standard library only, so the
workflow needs no pip install step.

Usage:
  python3 watch.py                 normal run: email new matches, update state
  python3 watch.py --dry-run       print the email instead of sending; state untouched
  python3 watch.py --test-email    email every matching open role; state untouched
  python3 watch.py --feed-file F   read Simplify listings from a local file (for testing)
  python3 watch.py --tesla-file F  read Tesla's feed from a local file (for testing)
  python3 watch.py --greenhouse-file F  read Greenhouse boards from a local file (for testing)
  python3 watch.py --ashby-file F       same for Ashby boards
  python3 watch.py --lever-file F       same for Lever boards

Email settings come from environment variables:
  SMTP_USER, SMTP_PASSWORD   login (for Gmail: your address and an app password)
  EMAIL_TO                   recipient, or several separated by commas
  EMAIL_FROM                 optional, defaults to SMTP_USER
  SMTP_HOST, SMTP_PORT       optional, default smtp.gmail.com:465
  SMTP_STARTTLS              optional, set to 0 to skip STARTTLS on non-465 ports
"""

from __future__ import annotations

import argparse
import bisect
import copy
import json
import os
import re
import smtplib
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
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
# Direct source: Tesla's careers site
# --------------------------------------------------------------------------- #

TESLA_FEED_URL = "https://www.tesla.com/cua-api/apps/careers/state"
TESLA_JOB_URL = "https://www.tesla.com/careers/search/job/{id}"
TESLA_JOB_ID = re.compile(r"tesla\.com/careers/search/job/(?:[^/?#]*-)?(\d{5,7})(?:[/?#]|$)")
# As of Nov 2026 Tesla answers scripts with HTTP 403 from any network, so the
# watcher reads Tesla through the Chrome extension's Gist (feed_url) instead.
# Direct fetching stays here in case Tesla ever opens the feed to scripts.
FEED_HEADERS = {
    "User-Agent": "internship-watch (https://github.com/arikurg/internship-watch)",
    "Accept": "application/json",
}
# If a direct source keeps failing for this long, send one email about it.
SOURCE_ALERT_AFTER = timedelta(hours=1)


class SourceError(Exception):
    pass


def tesla_job_id(url: str) -> str | None:
    """Job ID from a Tesla job link (both /job/289478 and /job/some-title-289478)."""
    match = TESLA_JOB_ID.search(url or "")
    return match.group(1) if match else None


def fetch_tesla_feed(attempts: int = 2) -> dict:
    last: SourceError | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(TESLA_FEED_URL, headers=FEED_HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            if not isinstance(data, dict) or "listings" not in data or "lookup" not in data:
                raise SourceError("got JSON, but not Tesla's job feed")
            return data
        except urllib.error.HTTPError as exc:
            last = SourceError(f"HTTP {exc.code} from Tesla")
        except json.JSONDecodeError:
            last = SourceError("Tesla sent a web page instead of JSON (probably a bot check)")
        except SourceError as exc:
            last = exc
        except Exception as exc:  # timeouts, DNS, connection resets
            last = SourceError(f"{type(exc).__name__}: {exc}")
        print(f"tesla fetch attempt {attempt} failed: {last}", file=sys.stderr)
        if attempt < attempts:
            time.sleep(5)
    raise last


def parse_terms(title: str) -> list[str]:
    """'Internship, X (Winter/Spring 2027)' -> ['Winter 2027', 'Spring 2027']."""
    match = re.search(r"\(([^()]*20\d\d[^()]*)\)\s*$", title)
    if not match:
        return []
    text = re.sub(r"([A-Za-z])(20\d\d)", r"\1 \2", match.group(1))
    parts = [p.strip() for p in text.split("/") if p.strip()]
    terms, year = [], None
    for part in reversed(parts):  # the year usually sits on the last season only
        found = re.search(r"20\d\d", part)
        if found:
            year = found.group(0)
        season = re.sub(r"\s*20\d\d\s*", " ", part).strip()
        terms.append(f"{season} {year}".strip() if year else season)
    return list(reversed(terms))


def tesla_date_estimator(known: dict[str, dict]):
    """Tesla publishes no posting dates, but its job IDs go up over time.
    Estimate a role's date from the Simplify dates of roles with nearby IDs."""
    pairs = sorted((int(job), l["date_posted"]) for job, l in known.items() if l.get("date_posted"))
    ids = [p[0] for p in pairs]

    def estimate(job_id: int, now_ts: float) -> tuple[float, bool]:
        if not pairs or job_id > ids[-1]:
            return now_ts, False  # newer than anything Simplify has: treat as just posted
        i = bisect.bisect_left(ids, job_id)
        guess = statistics.median(p[1] for p in pairs[max(0, i - 3): i + 3])
        # A role can't be posted after one with a higher ID.
        return min(guess, min(p[1] for p in pairs[i:])), True

    return estimate


def fetch_published_feed(url: str, max_age_hours: float) -> dict:
    """Read the Tesla feed that the Internship Watch Chrome extension publishes to a Gist."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "internship-watch"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SourceError("no Tesla feed found at feed_url. Open the Internship Watch extension's options in Chrome and click Save and check now.")
        raise SourceError(f"HTTP {exc.code} reading the published Tesla feed")
    except json.JSONDecodeError:
        raise SourceError("the published Tesla feed isn't valid JSON")
    except urllib.error.URLError as exc:
        raise SourceError(f"couldn't read the published Tesla feed: {exc.reason}")
    if not isinstance(data, dict) or "fetched_at" not in data or "listings" not in data:
        raise SourceError("the published Tesla feed is missing fields. Click Save and check now in the extension's options.")
    fetched = datetime.fromisoformat(data["fetched_at"])
    now = datetime.now(timezone.utc)
    if now - fetched > timedelta(hours=max_age_hours):
        raise SourceError(
            f"the Chrome extension last published Tesla's list {describe_age(fetched, now)}. It only runs "
            "while Chrome is open; open Chrome, or click Save and check now in the extension's options."
        )
    return data


def load_tesla(simplify: list[dict], feed_file: str | None = None, feed_url: str | None = None,
               max_age_hours: float = 24) -> list[dict]:
    """Tesla internships that Simplify doesn't list as open, in Simplify's format."""
    if feed_file:
        data = json.loads(Path(feed_file).read_text())
    elif feed_url:
        data = fetch_published_feed(feed_url, max_age_hours)
    else:
        data = fetch_tesla_feed()
    lookup = data["lookup"]
    intern_types = {str(k) for k, v in lookup.get("types", {}).items() if "intern" in str(v).lower()}
    if not intern_types:
        raise SourceError("Tesla's feed format changed: no internship job type found")
    locations = lookup.get("locations", {})
    departments = lookup.get("departments", {})

    known = {}  # Tesla job ID -> Simplify listing
    for listing in simplify:
        job = tesla_job_id(listing.get("url", ""))
        if job and listing.get("company_name", "").lower() == "tesla":
            known[job] = listing
    estimate = tesla_date_estimator(known)
    now_ts = time.time()

    # Tesla posts one role per location; collapse identical titles into one entry.
    groups: dict[str, list[dict]] = {}
    for raw in data["listings"]:
        if str(raw.get("y")) in intern_types and raw.get("t"):
            groups.setdefault(" ".join(raw["t"].split()), []).append(raw)

    out = []
    for title, raws in groups.items():
        ids = [str(r["id"]) for r in raws]
        on_simplify = [known[i] for i in ids if i in known]
        if any(l.get("active") for l in on_simplify):
            continue  # Simplify lists it as open, so its listing covers this role
        first = min(ids, key=int)
        posted, estimated = estimate(int(first), now_ts)
        department = departments.get(str(raws[0].get("dp")), "")
        out.append({
            "id": f"tesla:{first}",
            "company_name": "Tesla",
            "title": re.sub(r"^Internship,\s*", "", title),
            "url": TESLA_JOB_URL.format(id=first),
            "locations": list(dict.fromkeys(
                locations[str(r.get("l"))] for r in raws if str(r.get("l")) in locations)),
            "terms": parse_terms(title),
            "category": department,  # Tesla departments, not Simplify categories
            "degrees": [],
            "active": True,
            "is_visible": True,
            "date_posted": posted,
            "_date_estimated": estimated,
            "_source": "tesla",
            "_simplify": "closed" if on_simplify else "missing",
            "_aliases": [f"tesla:{i}" for i in ids] + [l["id"] for l in on_simplify],
        })
    return out


# --------------------------------------------------------------------------- #
# Direct sources: companies' job boards on Greenhouse, Ashby and Lever
# --------------------------------------------------------------------------- #

# All three publish a public job board API meant for programs, so GitHub's
# servers can read them directly. Tests point these at a local server.
GREENHOUSE_API = os.environ.get("GREENHOUSE_API_BASE", "https://boards-api.greenhouse.io/v1/boards")
ASHBY_API = os.environ.get("ASHBY_API_BASE", "https://api.ashbyhq.com/posting-api/job-board")
LEVER_API = os.environ.get("LEVER_API_BASE", "https://api.lever.co/v0/postings")
INTERN_TITLE = re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b", re.IGNORECASE)
SEASON_TERM = re.compile(
    r"\b(spring|summer|fall|autumn|winter)(?:\s*/\s*(spring|summer|fall|autumn|winter))?\s*(20\d\d)\b", re.IGNORECASE)
# Locations that are clearly outside the US. Job boards often list bare US city
# names ("San Francisco", "In-Office"), so for them a role only counts as
# non-US when every location matches this. Cities that share a name with a US
# city (London, Dublin, Paris, Berlin, Melbourne, Vancouver) are left out.
NON_US = re.compile(
    r"\b(?:united kingdom|england|scotland|wales|ireland|u\.?k\.?|canada|ontario|quebec|british columbia|alberta|"
    r"germany|france|netherlands|switzerland|sweden|denmark|norway|finland|poland|romania|czech|hungary|spain|"
    r"italy|israel|india|china|japan|korea|taiwan|singapore|australia|new zealand|mexico|brazil|philippines|"
    r"vietnam|united arab emirates|uae|toronto|montreal|sydney|tokyo|seoul|taipei|bangalore|bengaluru|"
    r"hyderabad|munich|tel aviv)\b", re.IGNORECASE)
US_COUNTRIES = {"us", "usa", "united states", "united states of america"}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def terms_in_title(title: str) -> list[str]:
    """Season terms anywhere in a title: 'Spring 2027 Software…', '…Internship - Spring 2027',
    '(Winter/Spring 2027)', '[Winter 2027]'."""
    terms = []
    for m in SEASON_TERM.finditer(title):
        for season in (m.group(1), m.group(2)):
            if season:
                terms.append(f"{season.capitalize()} {m.group(3)}")
    return terms


def looks_non_us(location: str) -> bool:
    return bool(NON_US.search(location)) and not is_us_location(location)


def parse_time(value) -> float | None:
    """Epoch milliseconds (Lever) or an ISO timestamp (Greenhouse, Ashby)."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def split_locations(*names) -> list[str]:
    out = []
    for name in names:
        for part in re.split(r"[;|]", name or ""):
            if part.strip() and part.strip() not in out:
                out.append(part.strip())
    return out


# Each board's jobs, reduced to: id, title, locations, countries, posted, url, intern.
def greenhouse_jobs(data) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("unexpected response")
    out = []
    for j in jobs:
        title = " ".join(str(j.get("title", "")).split())
        out.append({
            "id": str(j.get("id", "")), "title": title,
            "locations": split_locations((j.get("location") or {}).get("name")), "countries": [],
            "posted": parse_time(j.get("first_published")) or parse_time(j.get("updated_at")),
            "url": j.get("absolute_url"), "intern": bool(INTERN_TITLE.search(title)),
        })
    return out


def ashby_jobs(data) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("unexpected response")
    out = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        title = " ".join(str(j.get("title", "")).split())
        places = [j] + [s for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
        countries = [((p.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "" for p in places]
        out.append({
            "id": str(j.get("id", "")).lower(), "title": title,
            "locations": split_locations(*(p.get("location") for p in places)),
            "countries": countries if all(countries) else [],
            "posted": parse_time(j.get("publishedAt")),
            "url": j.get("jobUrl"),
            "intern": bool(INTERN_TITLE.search(title)) or str(j.get("employmentType", "")).lower() == "intern",
        })
    return out


def lever_jobs(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("unexpected response")
    out = []
    for j in data:
        title = " ".join(str(j.get("text", "")).split())
        cats = j.get("categories") or {}
        out.append({
            "id": str(j.get("id", "")).lower(), "title": title,
            "locations": split_locations(*(cats.get("allLocations") or [cats.get("location")])),
            "countries": [j["country"]] if j.get("country") else [],
            "posted": parse_time(j.get("createdAt")),
            "url": j.get("hostedUrl"),
            "intern": bool(INTERN_TITLE.search(title)) or "intern" in str(cats.get("commitment", "")).lower(),
        })
    return out


# kind -> how to read that job board site
BOARD_KINDS = {
    "greenhouse": {
        "label": "Greenhouse job boards", "prefix": "gh", "api": lambda t: f"{GREENHOUSE_API}/{t}/jobs",
        "parse": greenhouse_jobs, "job_id": r"\d{6,}",
        "board_url": r"greenhouse\.io/(?:embed/job_app\?for=)?{token}\b",
        "job_url": "https://job-boards.greenhouse.io/{token}/jobs/{id}",
    },
    "ashby": {
        "label": "Ashby job boards", "prefix": "ashby", "api": lambda t: f"{ASHBY_API}/{t}",
        "parse": ashby_jobs, "job_id": UUID,
        "board_url": r"ashbyhq\.com/{token}\b", "job_url": "https://jobs.ashbyhq.com/{token}/{id}",
    },
    "lever": {
        "label": "Lever job boards", "prefix": "lever", "api": lambda t: f"{LEVER_API}/{t}",
        "parse": lever_jobs, "job_id": UUID,
        "board_url": r"lever\.co/{token}\b", "job_url": "https://jobs.lever.co/{token}/{id}",
    },
}


def fetch_board(kind: str, token: str) -> list[dict]:
    spec = BOARD_KINDS[kind]
    try:
        req = urllib.request.Request(spec["api"](token), headers=FEED_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return spec["parse"](json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        hint = (f" (board not found: the company may have renamed it or left {kind.capitalize()})"
                if exc.code == 404 else "")
        raise SourceError(f"{token}: HTTP {exc.code}{hint}")
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError, AttributeError) as exc:
        raise SourceError(f"{token}: {exc}")


def load_boards(kind: str, simplify: list[dict], boards: dict[str, str],
                boards_file: str | None = None) -> tuple[list[dict], list[str]]:
    """Internships on one site's job boards that Simplify doesn't list as open,
    in Simplify's format. Returns (listings, errors for boards that failed)."""
    spec = BOARD_KINDS[kind]
    prefix = spec["prefix"]
    tracked = {name.lower() for name in boards}
    board_url = re.compile("|".join(spec["board_url"].format(token=re.escape(t)) for t in boards.values()),
                           re.IGNORECASE)
    job_id = re.compile(spec["job_id"], re.IGNORECASE)
    known: dict[str, dict] = {}  # job ID on this site -> Simplify listing
    for listing in simplify:
        url = listing.get("url", "")
        if listing.get("company_name", "").lower() in tracked or board_url.search(url):
            ids = [i.lower() for i in job_id.findall(url)]
            for i in ids:
                known[i] = listing
            # Lets a role already emailed from Simplify match its job board copy.
            listing.setdefault("_aliases", []).extend(f"{prefix}:{i}" for i in ids)

    saved = json.loads(Path(boards_file).read_text()) if boards_file else None

    def read(token: str) -> list[dict]:
        if saved is None:
            return fetch_board(kind, token)
        if token not in saved:
            raise SourceError(f"{token}: not in the test file")
        try:
            return spec["parse"](saved[token])
        except (ValueError, AttributeError) as exc:
            raise SourceError(f"{token}: {exc}")

    # Read the boards a few at a time so a long list doesn't slow the run.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {company: pool.submit(read, token) for company, token in boards.items()}

    out: list[dict] = []
    errors: list[str] = []
    for company, token in boards.items():
        try:
            jobs = futures[company].result()
        except SourceError as exc:
            errors.append(str(exc))
            continue

        # Companies often post one listing per location; merge identical titles.
        groups: dict[str, list[dict]] = {}
        for job in jobs:
            if job["id"] and job["intern"]:
                groups.setdefault(job["title"], []).append(job)

        for title, group in groups.items():
            ids = [j["id"] for j in group]
            on_simplify = [known[i] for i in ids if i in known]
            if any(l.get("active") for l in on_simplify):
                continue  # Simplify lists it as open, so its listing covers this role
            first = min(group, key=lambda j: j["posted"] or float("inf"))
            locations = split_locations(*(loc for j in group for loc in j["locations"]))
            countries = [c for j in group for c in j["countries"]]
            out.append({
                "id": f"{prefix}:{ids[0]}",
                "company_name": company,
                "title": title,
                "url": first["url"] or spec["job_url"].format(token=token, id=ids[0]),
                "locations": locations,
                "terms": terms_in_title(title),
                "category": "",
                "degrees": [],
                "active": True,
                "is_visible": True,
                "date_posted": first["posted"] or time.time(),
                "_date_known": bool(first["posted"]),
                "_lenient_location": True,
                # Ashby and Lever give countries; trust them when every location has one.
                "_non_us": bool(countries) and not any(c.strip().lower() in US_COUNTRIES for c in countries),
                "_source": kind,
                "_simplify": "closed" if on_simplify else "missing",
                "_aliases": [f"{prefix}:{i}" for i in ids] + [l["id"] for l in on_simplify],
            })
    return out, errors


# --------------------------------------------------------------------------- #
# Running the direct sources
# --------------------------------------------------------------------------- #

def source_health(name: str, label: str, sources_state: dict, now: datetime,
                  error: str | None) -> tuple[str, str] | None:
    """Track one source's failures so each outage is emailed once, plus once
    more when it recovers. Returns a notice to send, if any."""
    health = sources_state.setdefault(name, {})
    if error is None:
        notice = None
        if health.get("notified"):
            notice = (f"{label} working again",
                      f"The watcher can read the {label} again, so alerts from it are back on.")
        sources_state.pop(name, None)
        return notice
    notice = None
    if not health.get("failing_since"):
        health["failing_since"] = now.isoformat(timespec="seconds")
    elif not health.get("notified") and now - datetime.fromisoformat(health["failing_since"]) > SOURCE_ALERT_AFTER:
        notice = (f"{label} not working",
                  f"The watcher hasn't been able to read the {label} since {health['failing_since']} UTC.\n\n"
                  f"Last error: {error}\n\n"
                  "Simplify alerts still work. You'll get one more email when it works again.")
        health["notified"] = True
    health["last_error"] = error[:300]
    return notice


def run_direct_sources(args, config, simplify, sources_state, now) -> tuple[list[dict], list[tuple[str, str]]]:
    """Fetch the direct sources in config.json. Returns extra listings and notices to email."""
    extra: list[dict] = []
    notices: list[tuple[str, str]] = []
    direct = config.get("direct_sources", {})

    cfg = direct.get("tesla", {})
    if not cfg.get("enabled"):
        sources_state.pop("tesla", None)  # forget any old failure record
    else:
        error = None
        try:
            tesla = load_tesla(simplify, args.tesla_file, cfg.get("feed_url"), cfg.get("max_age_hours", 24))
            print(f"tesla: {len(tesla)} internships that Simplify doesn't list as open")
            extra += tesla
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            print(f"tesla source failed: {exc}", file=sys.stderr)
            hint = "" if cfg.get("feed_url") else (
                " Tesla blocks scripts, so set feed_url to the Chrome extension's Gist (see the README).")
            error = f"{exc}{hint}"
        notice = source_health("tesla", "Tesla job feed", sources_state, now, error)
        if notice:
            notices.append(notice)

    for kind, spec in BOARD_KINDS.items():
        cfg = direct.get(kind, {})
        if not cfg.get("enabled") or not cfg.get("boards"):
            sources_state.pop(kind, None)
            continue
        found, errors = load_boards(kind, simplify, cfg["boards"], getattr(args, f"{kind}_file", None))
        print(f"{kind}: {len(found)} internships that Simplify doesn't list as open"
              + (f" ({len(errors)} board(s) failed)" if errors else ""))
        for error in errors:
            print(f"{kind} board failed: {error}", file=sys.stderr)
        extra += found
        notice = source_health(kind, spec["label"], sources_state, now, "; ".join(errors) if errors else None)
        if notice:
            notices.append(notice)
    return extra, notices


def aliases(listing: dict) -> set[str]:
    """Every key one posting can appear under, so a role found on Tesla's site
    and later added to Simplify (or the reverse) only emails you once."""
    keys = {listing["id"], *listing.get("_aliases", [])}
    job = tesla_job_id(listing.get("url", ""))
    if job and listing.get("company_name", "").lower() == "tesla":
        keys.add(f"tesla:{job}")
    return keys


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def word_regex(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    alternatives = "|".join(re.escape(w.strip()) for w in words if w.strip())
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


US_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
    "puerto rico",
}
# Simplify's shorthand for big US metros. Bare "Remote" counts as US too.
US_SHORTHANDS = {"nyc", "sf", "la", "south sf", "bay area", "silicon valley", "washington dc", "remote"}
US_WORDS = re.compile(r"\b(?:united states|usa|u\.s\.a?|us)\b", re.IGNORECASE)


def is_us_location(location: str) -> bool:
    """True for Simplify location strings like "Seattle, WA", "NYC", "Texas" or "Remote in USA"."""
    text = location.strip()
    lower = text.lower()
    if US_WORDS.search(text) or lower in US_SHORTHANDS or lower in US_STATE_NAMES:
        return True
    parts = [p.strip() for p in text.split(",")]
    last = parts[-1]
    return (len(parts) >= 2 and last.upper() in US_STATE_CODES) or last.lower() in US_STATE_NAMES


class Watch:
    """One entry from config["watches"].

    A listing matches when its company is in scope and either its Simplify
    category is in `categories` or its title contains one of `title_keywords`.
    `title_exclude`, `degrees_any` and `us_only` fall back to the top-level
    config values.
    """

    def __init__(self, spec: dict, config: dict):
        self.name = spec["name"]
        companies = spec.get("companies", "any")
        self.companies = None if companies in ("any", "*", None) else {c.lower() for c in companies}
        self.categories = {c.lower() for c in spec.get("categories", [])}
        self.include = word_regex(spec.get("title_keywords", []))
        self.exclude = word_regex(spec.get("title_exclude", config.get("title_exclude", [])))
        self.degrees = {d.lower() for d in spec.get("degrees_any", config.get("degrees_any", []))}
        self.us_only = bool(spec.get("us_only", config.get("us_only", False)))

    def matches(self, listing: dict) -> bool:
        if self.companies is not None and listing.get("company_name", "").lower() not in self.companies:
            return False
        # A role passes if any of its locations is in the US (or it lists none).
        # Job boards often use bare city names, so there a role is only dropped
        # when the board says every location's country isn't the US, or every
        # location is clearly outside the US.
        locations = listing.get("locations") or []
        if self.us_only and locations:
            if listing.get("_lenient_location"):
                if listing.get("_non_us") or all(looks_non_us(l) for l in locations):
                    return False
            elif not any(is_us_location(l) for l in locations):
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


def save_state(seen: set[str], last_change: datetime, sources: dict | None = None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_change": last_change.isoformat(timespec="seconds"), "seen": sorted(seen)}
    if sources:
        payload["sources"] = sources
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


BADGES = {"missing": "Not on Simplify", "closed": "Simplify shows it closed"}


def posting_fields(p: dict, tz, now: datetime) -> dict:
    posted = datetime.fromtimestamp(p.get("date_posted", 0), tz=timezone.utc)
    age = describe_age(posted, now)
    if p.get("_source") and p.get("_date_known"):
        stamp = posted.astimezone(tz).strftime("%b %d, %Y").replace(" 0", " ")
        posted_text = f"Posted {stamp} ({age})"
    elif p.get("_source") and p.get("_date_estimated"):
        posted_text = f"Posted about {posted.astimezone(tz).strftime('%b %d').replace(' 0', ' ')} ({age}, estimated from job ID)"
    elif p.get("_source"):
        posted_text = f"Found on the company's site {age}"
    else:
        stamp = posted.astimezone(tz).strftime("%b %d, %I:%M %p %Z").replace(" 0", " ")
        posted_text = f"Posted {stamp} ({age})"
    return {
        "title": p.get("title", "(untitled)"),
        "company": p.get("company_name", ""),
        "url": p.get("url", ""),
        "locations": ", ".join(p.get("locations") or []) or "Location not listed",
        "terms": ", ".join(p.get("terms") or []) or "Term not listed",
        "posted": posted_text,
        "category": p.get("category", ""),
        "watch": p.get("_watch", ""),
        "badge": BADGES.get(p.get("_simplify", ""), ""),
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
    off_simplify = sum(1 for r in rows if r["badge"])
    if off_simplify and len(rows) > 1:
        subject += f" · {off_simplify} not on Simplify"
    elif off_simplify:
        subject += " (not on Simplify)"

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
        # Roles Simplify doesn't have go first: fewer people have seen them.
        group = sorted(group, key=lambda r: not r["badge"])
        text_lines += [f"== {name} ({len(group)}) ==", ""]
        html_parts.append(
            f'<tr><td style="padding:20px 0 4px;font-size:13px;font-weight:700;letter-spacing:.04em;'
            f'text-transform:uppercase;color:#555">{escape(name)} ({len(group)})</td></tr>'
        )
        for r in group:
            text_lines += [
                f"[{r['badge']}] {r['title']}" if r["badge"] else r["title"],
                f"  {r['company']} | {r['locations']} | {r['terms']}",
                f"  {r['posted']} | {r['category']}",
                f"  Apply: {r['url']}",
                "",
            ]
            badge_html = (
                f'<span style="display:inline-block;background:#fef3c7;color:#92400e;border-radius:4px;'
                f'padding:1px 6px;font-size:12px;font-weight:700;margin-bottom:4px">{escape(r["badge"])}</span><br>'
                if r["badge"] else ""
            )
            html_parts.append(f"""
        <tr><td style="padding:12px 0;border-bottom:1px solid #e5e5e5">
          {badge_html}<a href="{escape(r['url'])}" style="font-size:16px;font-weight:600;color:#1a56db;text-decoration:none">{escape(r['title'])}</a>
          <div style="color:#333;margin-top:4px"><b>{escape(r['company'])}</b> &middot; {escape(r['locations'])} &middot; {escape(r['terms'])}</div>
          <div style="color:#777;font-size:13px;margin-top:2px">{escape(r['posted'])} &middot; {escape(r['category'])}</div>
        </td></tr>""")
    source_names = {"tesla": "Tesla careers site", "greenhouse": "company Greenhouse boards",
                    "ashby": "company Ashby boards", "lever": "company Lever boards"}
    extra_sources = sorted({source_names[p["_source"]] for p in postings if p.get("_source") in source_names})
    footer = "Sources: " + ", ".join(["SimplifyJobs internship feed"] + extra_sources)
    text_lines.append(footer)
    text = "\n".join(text_lines)

    intro_html = escape(intro).replace("\n", "<br>")
    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:620px;margin:0 auto;padding:16px;color:#111">
  <p style="margin:0 0 8px">{intro_html}</p>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{''.join(html_parts)}</table>
  <p style="color:#999;font-size:12px;margin-top:16px">{footer}</p>
</body></html>"""
    return subject, text, html


def build_notice(subject: str, body: str) -> tuple[str, str, str]:
    html_body = escape(body).replace("\n", "<br>")
    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:620px;margin:0 auto;padding:16px;color:#111">
  <p>{html_body}</p>
</body></html>"""
    return f"[watcher] {subject}", body, html


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
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="internship-watch")
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


def deliver(postings, config, heading, dry_run, notice: tuple[str, str] | None = None) -> None:
    subject, text, html = build_notice(*notice) if notice else build_email(postings, config, heading)
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
    parser.add_argument("--tesla-file")
    parser.add_argument("--greenhouse-file", help="read Greenhouse boards from a local file (for testing)")
    parser.add_argument("--ashby-file", help="read Ashby boards from a local file (for testing)")
    parser.add_argument("--lever-file", help="read Lever boards from a local file (for testing)")
    args = parser.parse_args()
    if os.environ.get("TEST_EMAIL") in {"1", "true"}:
        args.test_email = True

    config = json.loads(CONFIG_PATH.read_text())
    now = datetime.now(timezone.utc)
    state = load_state()
    sources_before = (state or {}).get("sources", {})
    sources = copy.deepcopy(sources_before)

    listings = load_listings(args, config)
    direct, notices = run_direct_sources(args, config, listings, sources, now)

    matcher = Matcher(config)
    open_roles = []
    for listing in listings + direct:
        watch_name = matcher(listing)
        if watch_name:
            listing["_watch"] = watch_name
            open_roles.append(listing)
    open_roles.sort(key=lambda l: l.get("date_posted", 0), reverse=True)
    print(f"Simplify has {len(listings)} listings; {len(open_roles)} open roles match")
    for watch in matcher.watches:
        print(f"  {watch.name}: {sum(1 for l in open_roles if l['_watch'] == watch.name)}")
    print(f"  of those, not open on Simplify: {sum(1 for l in open_roles if l.get('_source'))}")

    if args.test_email:
        deliver(open_roles, config, "test", args.dry_run)
        return 0

    # Notices go out first: if email is broken, the run fails before anything
    # is recorded, and the next run tries again.
    for notice in notices:
        deliver([], config, "", args.dry_run, notice=notice)

    if state is None:
        print("first run: recording current roles as seen")
        if config.get("notify_on_first_run", True):
            deliver(open_roles, config, "live", args.dry_run)
        if not args.dry_run:
            save_state(set().union(*(aliases(l) for l in open_roles)), now, sources)
        return 0

    seen = set(state.get("seen", []))
    new = [l for l in open_roles if not aliases(l) & seen]
    print(f"{len(new)} new")
    last_change = datetime.fromisoformat(state.get("last_change", now.isoformat()))

    if new:
        # Send first. If the email fails, the run fails and the state stays
        # unchanged, so the next run retries the same postings.
        deliver(new, config, "new", args.dry_run)
        seen |= set().union(*(aliases(l) for l in new))
        last_change = now
    elif now - last_change > timedelta(days=HEARTBEAT_DAYS):
        print("no changes for a while: refreshing state to keep the schedule alive")
        last_change = now

    if not args.dry_run and (new or last_change == now or sources != sources_before):
        save_state(seen, last_change, sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())