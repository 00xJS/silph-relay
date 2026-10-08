"""Operational alerts, posted to Discord. Optional; silent when no webhook is set.

Why: the heartbeat monitor (healthchecks.io) is optional and, as of October
2026, not configured — so the 2026-10-05 GitHub Actions outage was only found
by looking. The Discord webhooks, on the other hand, always exist. An alert
goes to DISCORD_ALERT_WEBHOOK_URL (a private ops channel) when set, otherwise
to the relay's own channels, trying each until one accepts it — so a rejected
webhook can still be reported through the other.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import net

WEBHOOK_ENVS = ("DISCORD_ALERT_WEBHOOK_URL", "DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL_RESTOCKS")


def webhooks():
    urls = []
    for env in WEBHOOK_ENVS:
        url = (os.getenv(env) or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def notify(message, source="silph-relay"):
    """Post one alert. Returns True once a webhook accepted it. Never raises."""
    urls = webhooks()
    if not urls:
        print(f"[alerts] No Discord webhook configured — not sent: {message}")
        return False
    body = json.dumps({
        "username":         source,
        "content":          message[:1900],
        "allowed_mentions": {"parse": []},   # an ops notice never pings anyone
    }).encode("utf-8")
    for url in urls:
        try:
            r = net.request(url, "POST", {"Content-Type": "application/json"}, body, timeout=10)
            if r.ok:
                print(f"[alerts] Sent: {message}")
                return True
            print(f"[alerts] Discord answered HTTP {r.status} — trying the next webhook")
        except Exception as e:
            print(f"[alerts] Could not reach Discord: {e}")
    print(f"[alerts] No webhook accepted the alert: {message}")
    return False
