#!/usr/bin/env python3
"""Fetch Tesla's internship listings from this computer and publish them for the watcher.

Tesla refuses requests from GitHub's servers (HTTP 403) but answers ordinary
home and campus connections. Run this on your laptop on a timer: it downloads
Tesla's job feed, keeps the internship listings, and force-pushes them as
tesla.json to the `tesla-feed` branch of this repo. The GitHub Action reads
that file and does the matching, emailing and bookkeeping as usual.

Usage (from the repo folder):
  python3 fetch_tesla.py --check      fetch once and report what Tesla returned; publishes nothing
  python3 fetch_tesla.py              fetch and publish (what the timer runs)
  python3 fetch_tesla.py --install    run every 15 minutes in the background
  python3 fetch_tesla.py --uninstall  stop the background job

The `tesla-feed` branch holds a single commit that gets replaced on every
publish, so it never grows. Pushing it doesn't trigger any workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from watch import TESLA_FEED_URL, SourceError, fetch_tesla_feed  # noqa: E402

BRANCH = "tesla-feed"
INTERVAL_MINUTES = 15
LABEL = "com.internship-watch.tesla"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
MAC_LOG = Path.home() / "Library" / "Logs" / "internship-watch-tesla.log"
# Fewer internships than this means Tesla sent something odd; don't publish it.
MIN_INTERNSHIPS = 10


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}", flush=True)


def compact(data: dict) -> dict:
    """Keep only what the watcher needs: internship listings and their lookups."""
    lookup = data["lookup"]
    intern_types = {str(k) for k, v in lookup.get("types", {}).items() if "intern" in str(v).lower()}
    listings = [
        {k: x.get(k) for k in ("id", "t", "dp", "l", "y")}
        for x in data["listings"]
        if str(x.get("y")) in intern_types
    ]
    if len(listings) < MIN_INTERNSHIPS:
        raise SourceError(f"Tesla's feed had only {len(listings)} internships; not publishing it")
    departments = {str(x["dp"]) for x in listings}
    locations = {str(x["l"]) for x in listings}
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": TESLA_FEED_URL,
        "lookup": {
            "types": lookup["types"],
            "departments": {k: v for k, v in lookup.get("departments", {}).items() if k in departments},
            "locations": {k: v for k, v in lookup.get("locations", {}).items() if k in locations},
        },
        "listings": listings,
    }


def git(*args: str, data: bytes | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "internship-watch", "GIT_AUTHOR_EMAIL": "internship-watch@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "internship-watch", "GIT_COMMITTER_EMAIL": "internship-watch@users.noreply.github.com",
    }
    result = subprocess.run(["git", "-C", str(REPO), *args], input=data, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode().strip()}")
    return result.stdout.decode().strip()


def publish(feed: dict, remote: str = "origin") -> None:
    """Replace the tesla-feed branch with one commit holding tesla.json.
    Uses git plumbing, so your checkout and working files stay untouched."""
    body = (json.dumps(feed, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    blob = git("hash-object", "-w", "--stdin", data=body)
    tree = git("mktree", data=f"100644 blob {blob}\ttesla.json\n".encode())
    commit = git("commit-tree", tree, "-m", f"Tesla feed {feed['fetched_at']}")
    git("push", "--force", "--quiet", remote, f"{commit}:refs/heads/{BRANCH}")


# --------------------------------------------------------------------------- #
# Background job
# --------------------------------------------------------------------------- #

def install() -> None:
    python, script = sys.executable, str(Path(__file__).resolve())
    system = platform.system()
    if system == "Darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        MAC_LOG.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(launchd_job(python, script)))
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
        print(f"Installed. It runs now and every {INTERVAL_MINUTES} minutes while your Mac is awake.")
        print(f"Log: {MAC_LOG}")
        print(f"Watch it with: tail -f '{MAC_LOG}'")
    elif system == "Linux":
        logfile = Path.home() / ".internship-watch-tesla.log"
        print("Add this line with `crontab -e`:\n")
        print(f"*/{INTERVAL_MINUTES} * * * * '{python}' '{script}' >> '{logfile}' 2>&1")
    elif system == "Windows":
        print("Run this in Command Prompt:\n")
        print(f'schtasks /Create /SC MINUTE /MO {INTERVAL_MINUTES} /TN InternshipWatchTesla '
              f'/TR "\\"{python}\\" \\"{script}\\""')
    else:
        print(f"Schedule `{python} {script}` to run every {INTERVAL_MINUTES} minutes.")


def launchd_job(python: str, script: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, script],
        "WorkingDirectory": str(REPO),
        "StartInterval": INTERVAL_MINUTES * 60,
        "RunAtLoad": True,
        # launchd starts with a bare PATH; git's credential helpers (gh, osxkeychain) need these.
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(MAC_LOG),
        "StandardErrorPath": str(MAC_LOG),
    }


def uninstall() -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
        PLIST.unlink(missing_ok=True)
        print("Removed the background job.")
    elif system == "Linux":
        print("Delete the fetch_tesla.py line with `crontab -e`.")
    elif system == "Windows":
        print("Run: schtasks /Delete /TN InternshipWatchTesla /F")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fetch and report; publish nothing")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--from-file", help=argparse.SUPPRESS)  # testing: use a saved full feed
    args = parser.parse_args()

    if args.install:
        install()
        return 0
    if args.uninstall:
        uninstall()
        return 0

    try:
        data = json.loads(Path(args.from_file).read_text()) if args.from_file else fetch_tesla_feed()
        feed = compact(data)
    except (SourceError, KeyError, ValueError) as exc:
        log(f"Tesla fetch failed: {exc}")
        return 1

    log(f"Tesla answered: {len(data['listings'])} listings, {len(feed['listings'])} internships")
    if args.check:
        print("Tesla accepts requests from this computer. Next: python3 fetch_tesla.py --install")
        return 0
    try:
        publish(feed)
    except RuntimeError as exc:
        log(f"Publishing failed: {exc}")
        return 1
    log(f"Published tesla.json to the {BRANCH} branch")
    return 0


if __name__ == "__main__":
    sys.exit(main())