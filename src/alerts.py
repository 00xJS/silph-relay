"""Operational alerts, posted to the Pokémon GO channel's webhook.

Why: the heartbeat monitor (healthchecks.io) is optional and, as of October
2026, not configured — so the 2026-10-05 GitHub Actions outage was only found
by looking. The Discord webhook, on the other hand, always exists. Alerts are
rare (one when a problem starts, one when it ends) and never ping anyone.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import net

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def notify(message, source="silph-relay"):
    """Post one alert. Returns True if Discord accepted it. Never raises."""
    url = (os.getenv(WEBHOOK_ENV) or "").strip()
    if not url:
        print(f"[alerts] {WEBHOOK_ENV} not set — not sent: {message}")
        return False
    body = json.dumps({
        "username":         source,
        "content":          message[:1900],
        "allowed_mentions": {"parse": []},   # an ops notice never pings anyone
    }).encode("utf-8")
    try:
        r = net.request(url, "POST", {"Content-Type": "application/json"}, body, timeout=10)
        if r.ok:
            print(f"[alerts] Sent: {message}")
            return True
        print(f"[alerts] Discord answered HTTP {r.status} — not sent: {message}")
    except Exception as e:
        print(f"[alerts] Could not reach Discord: {e} — not sent: {message}")
    return False
