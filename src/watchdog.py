"""Relay watchdog: cancel relay runs that GitHub queued but never ran.

Why: on 2026-09-12 a relay run's job never got a runner. GitHub left it queued
for exactly 24 hours — and because it held the relay's concurrency group, every
dispatch behind it (~1,400) was cancelled: a full day of silence, and no failure
email, since cancellations don't send one. `timeout-minutes` can't help; it only
counts once a job is running.

This runs from its own workflow with NO concurrency group, so a stuck relay run
can never block it. It only acts when the relay has actually stalled — no
successful run for STALL_AFTER — and then cancels every relay run still
unfinished after STUCK_AFTER (a normal run takes under a minute). That's safe
for a job that never started — nothing was posted or marked seen, so the next
dispatch picks up the same posts — and long past `timeout-minutes: 3` for one
that did. A run that ignores the cancel is force-cancelled on a later pass.

The stall gate matters: GitHub's API also lists a few "ghost" runs, queued with
zero jobs since the 2026-09-11/13 incidents, that block nothing. While the
relay is healthy they're left alone instead of being poked every 5 minutes.

Once an hour it also checks that the Nitter fallback still answers. A fallback
nobody exercises rots unnoticed — that's how nitter.net's death went unseen —
so this pings HEALTHCHECK_FALLBACK_URL (optional) while at least one works.
"""
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(__file__))

import net
from fetcher import NITTER_INSTANCES, NITTER_TIMEOUT

API            = (os.getenv("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
REPO           = os.getenv("GITHUB_REPOSITORY") or "00xJS/silph-relay"
TOKEN          = os.getenv("GITHUB_TOKEN") or ""
RELAY_WORKFLOW = os.getenv("RELAY_WORKFLOW") or "pipeline.yml"
DRY_RUN        = (os.getenv("WATCHDOG_DRY_RUN") or "").strip().lower() in ("1", "true", "yes")

# The relay succeeds every minute, and GitHub's worst runner-allocation delay
# seen so far (~5 min) resolved on its own — so 10 minutes without a success is
# a stall, and a run unfinished for 10 minutes is stuck.
STALL_AFTER  = 10 * 60
STUCK_AFTER  = 10 * 60
FORCE_AFTER  = 20 * 60   # still there after a normal cancel: force it
JUST_STARTED = 4 * 60    # a job this fresh on a runner is working — leave it

CANARY_ACCOUNT = "LeekDuck"  # posts daily, so a working instance always lists something


def gh(path, method="GET"):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return net.request(f"{API}{path}", method, headers, timeout=20)


def seconds_since(stamp):
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return time.time() - then.timestamp()


def unfinished_relay_runs():
    """Relay runs holding (or about to hold) the concurrency group.

    Runs merely waiting their turn behind another are "pending" and aren't
    listed: they're harmless, and a newer dispatch replaces them each minute.
    """
    runs = {}
    for status in ("queued", "in_progress"):
        r = gh(f"/repos/{REPO}/actions/workflows/{RELAY_WORKFLOW}/runs?status={status}&per_page=100")
        if not r.ok:
            raise RuntimeError(f"listing {status} relay runs failed: HTTP {r.status} {r.text[:200]}")
        for run in r.json().get("workflow_runs", []):
            runs[run["id"]] = run
    return sorted(runs.values(), key=lambda run: run["created_at"])


def just_started(run_id):
    """True if a job of this run is on a runner and started moments ago."""
    r = gh(f"/repos/{REPO}/actions/runs/{run_id}/jobs")
    if not r.ok:
        return False
    for job in r.json().get("jobs", []):
        if job.get("runner_name") and job.get("started_at") and seconds_since(job["started_at"]) < JUST_STARTED:
            return True
    return False


def last_success_age():
    """Seconds since the relay last completed a run successfully (None if never)."""
    r = gh(f"/repos/{REPO}/actions/workflows/{RELAY_WORKFLOW}/runs?status=success&per_page=1")
    if not r.ok:
        raise RuntimeError(f"listing successful relay runs failed: HTTP {r.status} {r.text[:200]}")
    runs = r.json().get("workflow_runs", [])
    return seconds_since(runs[0]["updated_at"]) if runs else None


def unstick():
    """If the relay has stalled, cancel the runs holding it up. Returns what was done."""
    age = last_success_age()
    if age is not None and age < STALL_AFTER:
        print(f"[watchdog] Relay healthy — last successful run {age:.0f}s ago")
        return []
    print(f"[watchdog] Relay stalled — "
          + (f"no successful run for {age / 60:.0f} min" if age is not None else "no successful run on record"))

    done, planned = [], 0
    for run in unfinished_relay_runs():
        age = seconds_since(run["created_at"])
        if age < STUCK_AFTER:
            continue
        what = f"relay run {run['id']} ({run['status']} for {age / 60:.0f} min, created {run['created_at']})"
        if just_started(run["id"]):
            print(f"[watchdog] Leaving {what} — its job only just got a runner")
            continue

        action = "force-cancel" if age >= FORCE_AFTER else "cancel"
        planned += 1
        if DRY_RUN:
            print(f"[watchdog] DRY RUN — would {action} {what}")
            continue
        r = gh(f"/repos/{REPO}/actions/runs/{run['id']}/{action}", "POST")
        if r.ok:
            line = f"{action.capitalize()}led stuck {what}"
            print(f"[watchdog] {line}")
            print(f"::warning::{line}")
            done.append(line)
        else:
            print(f"::error::Could not {action} {what}: HTTP {r.status} {r.text[:200]}")
    if not planned:
        print("[watchdog] No stuck relay run found — the stall is elsewhere (dispatch token, cron-job.org, GitHub)")
    return done


def report(url, ok, message):
    """Ping a healthchecks.io check (ok) or add a log line to it (not ok)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return
    try:
        net.request(url if ok else f"{url}/log", "POST",
                    {"Content-Type": "text/plain; charset=utf-8"},
                    message.encode("utf-8")[:10000], timeout=10)
    except Exception as e:
        print(f"[watchdog] Could not reach the monitor: {e}")


def check_fallback():
    """Confirm at least one Nitter instance still serves RSS. Returns True if so."""
    results = []
    for base in NITTER_INSTANCES:
        host = urlsplit(base).hostname or base
        try:
            r = net.request(f"{base}/{CANARY_ACCOUNT}/rss", timeout=NITTER_TIMEOUT)
            if not r.ok:
                raise OSError(f"HTTP {r.status}")
            channel = ET.fromstring(r.body).find("channel")
            items = channel.findall("item") if channel is not None else []
            if not items:
                raise ValueError("feed had no entries")
            results.append((host, True, f"{len(items)} posts"))
        except Exception as e:
            results.append((host, False, str(e)[:120]))

    summary = "; ".join(f"{host}: {'OK' if ok else 'FAILED'} ({detail})" for host, ok, detail in results)
    print(f"[watchdog] Fallback check — {summary or 'no instances configured'}")
    for host, ok, detail in results:
        if not ok:
            print(f"::warning::Nitter fallback {host} is not answering: {detail}")
    working = any(ok for _, ok, _ in results)
    if not working:
        print("::error::No Nitter fallback instance is answering — the relay has no backup source")
    report(os.getenv("HEALTHCHECK_FALLBACK_URL"), working, summary or "no instances configured")
    return working


def canary_due():
    """Hourly on the 5-minute dispatch schedule; always on GitHub's rare cron runs."""
    if (os.getenv("WATCHDOG_CANARY") or "").strip() == "1":
        return True
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        return True
    return datetime.now(timezone.utc).minute < 5


def main():
    try:
        done = unstick()
        if done:
            report(os.getenv("HEALTHCHECK_URL"), False, "watchdog: " + "\n".join(done))
    except Exception as e:
        print(f"::error::Watchdog could not check relay runs: {e}")

    if canary_due():
        check_fallback()
    return 0  # never fail: this runs every few minutes, and failure emails would pile up


if __name__ == "__main__":
    sys.exit(main())
