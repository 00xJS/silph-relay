"""Report the run to a heartbeat monitor (healthchecks.io). Optional.

Does nothing unless the HEALTHCHECK_URL secret is set.

A healthy run pings the check. An unhealthy one — every source down, or a
Discord webhook rejected: things a green run would otherwise hide — only adds
a log line to the check and withholds the ping. Either way, if pings stop the
check goes DOWN once its grace period passes and the monitor alerts. That also
covers runs that never happen at all: a job stuck in GitHub's queue (the
2026-09-12 outage), an expired dispatch token, cron-job.org being down. A
single bad minute pages no one; ten do.

The workflow only runs this step when every earlier step succeeded.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import net

HEALTH_FILE = Path("data/.health.json")


def main():
    url = (os.getenv("HEALTHCHECK_URL") or "").strip().rstrip("/")
    if not url:
        print("[heartbeat] HEALTHCHECK_URL not set — skipping")
        return 0

    try:
        report = json.loads(HEALTH_FILE.read_text())
    except Exception as e:
        report = {"ok": False, "summary": "", "problems": [f"the relay left no health report ({e})"]}

    ok = bool(report.get("ok"))
    lines = [report.get("summary") or ""] + list(report.get("problems") or [])
    body = "\n".join(line for line in lines if line).encode("utf-8")[:10000]

    # /log records a message without changing the check's state
    target = url if ok else f"{url}/log"
    for _ in range(2):
        try:
            r = net.request(target, "POST", {"Content-Type": "text/plain; charset=utf-8"}, body, timeout=10)
            if r.ok:
                print(f"[heartbeat] {'Pinged' if ok else 'Logged a problem to'} the monitor")
                return 0
            print(f"[heartbeat] Monitor answered HTTP {r.status}")
        except Exception as e:
            print(f"[heartbeat] Could not reach the monitor: {e}")
    return 0  # monitoring must never fail the run


if __name__ == "__main__":
    sys.exit(main())
