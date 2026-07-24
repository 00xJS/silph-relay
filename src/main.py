import os
import re
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Allow running from repo root
sys.path.insert(0, os.path.dirname(__file__))

import config
from fetcher import fetch_all, current_ids, setup as source_setup
from tracker import load_seen_ids, save_seen_ids
from discord_poster import post_to_discord

# Shared health state for /health
_state = {
    "ok": True,
    "last_run_at": None,
    "last_error": None,
    "runs": 0,
    "posted_total": 0,
}

# Per-tweet failure counters so a permanently-bad post is eventually dropped
# instead of blocking the loop forever.
_fail_counts = {}


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logs — UptimeRobot hits this every few minutes
        return

    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = (
                f"ok={_state['ok']}\n"
                f"runs={_state['runs']}\n"
                f"posted_total={_state['posted_total']}\n"
                f"last_run_at={_state['last_run_at']}\n"
                f"last_error={_state['last_error'] or ''}\n"
            ).encode()
            code = 200 if _state["ok"] else 503
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start_health_server(port):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[main] Health server listening on 0.0.0.0:{port}")
    return server


def _canonical(tweet_id):
    """Normalize a seen ID to its numeric tweet ID.

    The old RSSHub pipeline stored full URLs (…/status/123); twscrape yields the
    numeric 123. Canonicalizing on load lets existing history match so the
    source switch doesn't repost everything.
    """
    s = str(tweet_id)
    if s.isdigit():
        return s
    m = re.search(r"/status/(\d+)", s)
    return m.group(1) if m else s


def _publish(posts, seen_ids):
    """Post each tweet, update seen_ids in place, clean up temp images."""
    posted = 0
    for post in posts:
        display = post["account"]["display"]
        channel = post["account"].get("webhook", "updates")
        print(f"  [main] Posting {display} → {channel} — {post['id']}")

        success = post_to_discord(post, webhook_url=post.get("webhook_url"))

        if success:
            seen_ids.add(post["id"])
            posted += 1
            print("  [main] ✓ Posted")
        else:
            count = _fail_counts.get(post["id"], 0) + 1
            _fail_counts[post["id"]] = count
            if count >= config.MAX_POST_RETRIES:
                seen_ids.add(post["id"])  # give up so we stop retrying forever
                print(f"  [main] ✗ Giving up on {post['id']} after {count} tries")
            else:
                print(f"  [main] ✗ Failed (attempt {count})")

        for path, _name in post.get("images", []):
            try:
                os.remove(path)
            except OSError:
                pass

        time.sleep(config.DISCORD_POST_DELAY)

    return posted


def _mark_run(error=None):
    _state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _state["runs"] += 1
    _state["ok"] = error is None
    _state["last_error"] = error


def poll_once(seen_ids):
    """One fetch → post → save cycle against an in-memory seen set."""
    previous = set(seen_ids)
    posts = fetch_all(seen_ids)

    if posts:
        print(f"[main] {len(posts)} new posts to publish")
        posted = _publish(posts, seen_ids)
        _state["posted_total"] += posted

    save_seen_ids(seen_ids, previous=previous)
    _mark_run()
    return len(posts)


def prime_if_empty(seen_ids):
    """Fresh install: mark existing tweets seen without posting them."""
    if seen_ids or not config.PRIME_ON_EMPTY:
        return
    ids = current_ids()
    seen_ids.update(ids)
    save_seen_ids(seen_ids, previous=set())
    print(f"[main] Primed {len(ids)} existing tweets (won't repost)")


def bootstrap():
    """Load + canonicalize seen IDs and initialize the source once."""
    source_setup()
    seen_ids = {_canonical(i) for i in load_seen_ids()}
    print(f"[main] {len(seen_ids)} post IDs already seen")
    prime_if_empty(seen_ids)
    return seen_ids


def run_loop():
    """Long-running poll loop for Render (or any always-on host)."""
    start_health_server(config.PORT)
    interval = config.POLL_INTERVAL_SECONDS
    print(f"[main] Loop mode — polling every ~{interval}s "
          f"(set RUN_ONCE=1 for a single pass)")

    seen_ids = bootstrap()

    while True:
        try:
            poll_once(seen_ids)
        except Exception as e:  # noqa: BLE001 - never let the loop die
            _mark_run(error=str(e))
            print(f"[main] Run error: {e}")

        # Light jitter so we aren't a perfectly periodic (bot-obvious) caller.
        time.sleep(interval + (time.time() % max(1, config.JITTER)))


def run_once():
    """Single pass then exit (local test / manual GitHub Action)."""
    print("[main] Single-pass run…")
    seen_ids = bootstrap()
    try:
        n = poll_once(seen_ids)
        print(f"[main] Done — {n} new posts processed")
    except Exception as e:  # noqa: BLE001
        _mark_run(error=str(e))
        print(f"[main] Run error: {e}")


def main():
    print("[main] silph-relay starting")
    print(f"[main] mode={'run-once' if config.RUN_ONCE else 'loop'} "
          f"accounts={len(config.ACCOUNTS)} "
          f"poll={config.POLL_INTERVAL_SECONDS}s sweep={config.SWEEP_INTERVAL}s")
    if config.RUN_ONCE:
        run_once()
    else:
        run_loop()


if __name__ == "__main__":
    main()
