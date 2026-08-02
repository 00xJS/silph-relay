import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# Max images Discord accepts per message
MAX_IMAGES = 10

# Pacing: Discord's docs say not to hard-code rate limits — read the response
# headers instead. We only sleep when a webhook's bucket is actually exhausted.
# Per-webhook state: {webhook_url: (remaining, ready_at_monotonic)}
_pacing = {}

# Fallback delay when Discord returns no rate-limit headers (docs say they're
# present on "most", not all, responses)
BLIND_DELAY   = 1.0
MAX_PACE_WAIT = 5.0    # never pre-emptively sleep longer than this
MAX_RETRY_WAIT = 30.0  # a 429 asking for longer than this: give up, retry next run
MAX_RETRIES    = 2

_logged_headers = False


def _wait_turn(webhook_url):
    """Sleep only if this webhook's bucket is known to be exhausted."""
    remaining, ready_at = _pacing.get(webhook_url, (None, 0.0))
    if remaining is None:
        return  # first request to this webhook — go
    if remaining >= 1:
        return  # bucket has room — go
    delay = min(max(0.0, ready_at - time.monotonic()), MAX_PACE_WAIT)
    if delay > 0:
        print(f"  [discord] Bucket empty — pacing {delay:.2f}s")
        time.sleep(delay)


def _record_headers(webhook_url, r):
    """Remember how much bucket room is left, for the next post to this webhook."""
    global _logged_headers
    h = r.headers
    if not _logged_headers:
        _logged_headers = True
        print(f"  [discord] Rate-limit headers: limit={h.get('X-RateLimit-Limit')} "
              f"remaining={h.get('X-RateLimit-Remaining')} "
              f"reset_after={h.get('X-RateLimit-Reset-After')}")
    try:
        remaining  = int(h["X-RateLimit-Remaining"])
        reset_after = float(h["X-RateLimit-Reset-After"])
    except (KeyError, TypeError, ValueError):
        # No headers — fall back to a blind delay before the next send
        _pacing[webhook_url] = (0, time.monotonic() + BLIND_DELAY)
        return
    _pacing[webhook_url] = (remaining, time.monotonic() + reset_after)


def _retry_after(r):
    """Seconds to wait per a 429 response. Body is float seconds, not ms."""
    try:
        return float(r.json().get("retry_after"))
    except Exception:
        try:
            return float(r.headers.get("Retry-After", BLIND_DELAY))
        except (TypeError, ValueError):
            return BLIND_DELAY


def post_to_discord(post):
    """
    Post a tweet to Discord via the webhook configured for its account.
    Sends text as message content and attaches images as files.

    Returns True on success, False on a permanent failure (the post is marked
    seen so we don't retry forever), and None when the post should be RETRIED
    on a later run (no webhook configured, rate limited, or network error) —
    None leaves it unseen.
    """
    account = post["account"]
    text    = post["text"] or ""
    url     = post["url"]
    images  = post["images"]  # list of (tmp_path, filename)

    webhook_env = account.get("webhook_env", "DISCORD_WEBHOOK_URL")
    webhook_url = os.getenv(webhook_env, "")
    if not webhook_url:
        print(f"  [discord] No webhook configured ({webhook_env}) — skipping")
        return None

    # Build message content
    content = f"**{account['display']}**"
    if url:
        content += f"  |  [View Post]({url})"
    if text:
        content += f"\n\n{text}"

    # Cap at Discord's 2000 char limit
    if len(content) > 2000:
        content = content[:1997] + "..."

    for attempt in range(MAX_RETRIES + 1):
        _wait_turn(webhook_url)

        files = {}
        try:
            if images:
                # Send with file attachments (up to MAX_IMAGES)
                for i, (path, name) in enumerate(images[:MAX_IMAGES]):
                    files[f"files[{i}]"] = (name, open(path, "rb"))

                payload = {"content": content, "flags": 4}
                r = requests.post(webhook_url, data={"payload_json": json.dumps(payload)},
                                  files=files, timeout=30)
            else:
                # Text-only post
                r = requests.post(webhook_url, json={"content": content, "flags": 4}, timeout=30)
        except Exception as e:
            print(f"  [discord] Exception posting {post['id']}: {e}")
            return None  # transient — retry on a later run
        finally:
            for _, fh in files.values():
                try:
                    fh.close()
                except Exception:
                    pass

        _record_headers(webhook_url, r)

        if r.status_code in (200, 204):
            return True

        if r.status_code == 429:
            # No rate-limit headers on a 429 means a Cloudflare/shared-IP block
            # (GitHub runners share egress IPs) — backing off harder won't help
            # this run, so bail and let a later run retry.
            if "X-RateLimit-Remaining" not in r.headers:
                print("  [discord] 429 with no rate-limit headers — IP-level block, aborting")
                return None
            wait = _retry_after(r)
            if wait > MAX_RETRY_WAIT or attempt == MAX_RETRIES:
                print(f"  [discord] Rate limited (retry_after={wait:.1f}s) — will retry next run")
                return None
            print(f"  [discord] Rate limited — waiting {wait:.1f}s and retrying")
            time.sleep(wait)
            continue

        if r.status_code >= 500:
            print(f"  [discord] Server error ({r.status_code}) — will retry next run")
            return None

        print(f"  [discord] Failed ({r.status_code}): {r.text[:200]}")
        return False

    return None
