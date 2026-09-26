"""Send a relayed post to Discord through its channel's webhook."""
import json
import os
import re
import time

import envfile
import net
from fetcher import status_num, tweet_time

envfile.load()

# Max images Discord accepts per message, and its message length limit
MAX_IMAGES  = 10
MAX_CONTENT = 2000

# Discord asks API clients to identify themselves in this form
DISCORD_UA = "DiscordBot (https://github.com/00xJS/silph-relay, 2.0)"

# Post as the tracked account — its handle as the webhook name, its X avatar —
# so a channel reads like a feed rather than a wall of identical bot messages.
# Set the DISCORD_ACCOUNT_IDENTITY variable to 0 for the original look (bold
# handle in the text, under the webhook's own name and avatar).
ACCOUNT_IDENTITY = (os.getenv("DISCORD_ACCOUNT_IDENTITY") or "1").strip().lower() not in ("0", "false", "no", "off")

# Optional: a role to mention on fresh restock posts so members get a push
# notification. Only posts routed to the restocks webhook ping — never late ones.
RESTOCK_ROLE_ID     = re.sub(r"\D", "", os.getenv("DISCORD_RESTOCK_ROLE_ID") or "")
RESTOCK_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_RESTOCKS"

# A post delivered this long after it was tweeted gets a "posted … ago" stamp
# (Discord renders it in each reader's own time), so a restock relayed after an
# outage isn't mistaken for a live drop.
LATE_POST_SECONDS = float(os.getenv("LATE_POST_SECONDS") or 600)

# Webhooks Discord refused this run (401/403/404). main.py reports them, since
# nothing reaches that channel until the secret is fixed.
REJECTED_WEBHOOKS = set()

# Pacing: Discord's docs say not to hard-code rate limits — read the response
# headers instead. We only sleep when a webhook's bucket is actually exhausted.
# Per-webhook state: {webhook_url: (remaining, ready_at_monotonic)}
_pacing = {}

# Fallback delay when Discord returns no rate-limit headers (docs say they're
# present on "most", not all, responses)
BLIND_DELAY    = 1.0
MAX_PACE_WAIT  = 5.0   # never pre-emptively sleep longer than this
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
        remaining   = int(h.get("X-RateLimit-Remaining"))
        reset_after = float(h.get("X-RateLimit-Reset-After"))
    except (TypeError, ValueError):
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


def webhook_name(account):
    """The account's handle, as a webhook display name.

    Discord refuses names containing '@', '#', ':', '```', 'discord' or
    'clyde', so the bare handle is used and scrubbed just in case.
    """
    name = account["handle"]
    for bad in ("@", "#", ":", "```"):
        name = name.replace(bad, "")
    name = re.sub(r"(?i)discord|clyde", "", name).strip()
    return name[:80] or None


def build_payload(post, identity=ACCOUNT_IDENTITY, now=None):
    """The webhook JSON for a post. Attachments are sent alongside it."""
    account = post["account"]
    url     = post.get("url") or ""
    text    = post.get("text") or ""
    now     = time.time() if now is None else now

    created = tweet_time(status_num(post["id"]))
    late    = created is not None and now - created > LATE_POST_SECONDS

    line = f"[View Post]({url})" if url else ""
    if not identity:
        line = f"**{account['display']}**" + (f"  |  {line}" if line else "")

    notes = []
    author = (post.get("author") or "").strip()
    if author and author.lower() != account["handle"].lower():
        notes.append(f"🔁 reposted @{author}")
    if late:
        notes.append(f"🕑 posted <t:{int(created)}:R>")
    line = "  ·  ".join(part for part in [line] + notes if part)

    ping = RESTOCK_ROLE_ID and not late and account.get("webhook_env") == RESTOCK_WEBHOOK_ENV
    if ping:
        line = f"<@&{RESTOCK_ROLE_ID}> {line}"

    content = line + (f"\n\n{text}" if text else "")
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT - 1] + "…"

    payload = {
        "content": content,
        "flags": 4,  # SUPPRESS_EMBEDS — no link-preview card under every post
        # Tweet text can contain "@everyone" or "@here": never let it ping
        # anyone. Only the configured restock role may be mentioned.
        "allowed_mentions": {"parse": [], "roles": [RESTOCK_ROLE_ID]} if ping else {"parse": []},
    }
    if identity:
        name = webhook_name(account)
        if name:
            payload["username"] = name
        if post.get("avatar"):
            payload["avatar_url"] = post["avatar"]
    return payload


def _send(webhook_url, webhook_env, post, payload, images):
    """Deliver one payload, riding out rate limits. Returns (result, last HTTP status)."""
    for attempt in range(MAX_RETRIES + 1):
        _wait_turn(webhook_url)

        headers = {"User-Agent": DISCORD_UA}
        if images:
            body, headers["Content-Type"] = net.multipart(
                [("payload_json", json.dumps(payload), "application/json")],
                [(f"files[{i}]", name, data, ctype) for i, (name, data, ctype) in enumerate(images)])
        else:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            r = net.request(webhook_url, "POST", headers, body, timeout=30)
        except Exception as e:
            print(f"  [discord] Exception posting {post['id']}: {e}")
            return None, None  # transient — retry on a later run

        _record_headers(webhook_url, r)

        if r.status in (200, 204):
            return True, r.status

        if r.status == 429:
            # No rate-limit headers on a 429 means a Cloudflare/shared-IP block
            # (GitHub runners share egress IPs) — backing off harder won't help
            # this run, so bail and let a later run retry.
            if "X-RateLimit-Remaining" not in r.headers:
                print("  [discord] 429 with no rate-limit headers — IP-level block, aborting")
                return None, r.status
            wait = _retry_after(r)
            if wait > MAX_RETRY_WAIT or attempt == MAX_RETRIES:
                print(f"  [discord] Rate limited (retry_after={wait:.1f}s) — will retry next run")
                return None, r.status
            print(f"  [discord] Rate limited — waiting {wait:.1f}s and retrying")
            time.sleep(wait)
            continue

        if r.status >= 500:
            print(f"  [discord] Server error ({r.status}) — will retry next run")
            return None, r.status

        # 401/403/404 mean the WEBHOOK is broken (revoked, deleted, wrong URL),
        # not that this post is bad. Marking it seen would silently destroy it
        # and every post after it, behind a green checkmark — so defer instead
        # and make the reason loud.
        if r.status in (401, 403, 404):
            REJECTED_WEBHOOKS.add(webhook_env)
            print(f"  [discord] !! Webhook rejected the request ({r.status}) via {webhook_env}. "
                  f"The webhook is probably deleted or the secret is wrong — nothing will post "
                  f"to this channel until it's fixed. Holding the post for retry.")
            return None, r.status

        print(f"  [discord] Failed ({r.status}): {r.text[:200]}")
        return False, r.status

    return None, 429


def post_to_discord(post):
    """
    Post a tweet to Discord via the webhook configured for its account.
    Sends text as message content and attaches images as files.

    Returns True on success, False on a permanent failure (the post is marked
    seen so we don't retry forever), and None when the post should be RETRIED
    on a later run (no webhook configured, rate limited, network error, or a
    rejected webhook) — None leaves it unseen.
    """
    webhook_env = post["account"].get("webhook_env", "DISCORD_WEBHOOK_URL")
    webhook_url = os.getenv(webhook_env, "")
    if not webhook_url:
        print(f"  [discord] No webhook configured ({webhook_env}) — skipping")
        return None

    images = (post.get("images") or [])[:MAX_IMAGES]

    # Send the full message first. If Discord refuses its shape (400/413 — an
    # avatar it won't take, attachments too large), step down to plainer
    # versions instead of losing the post: only a refusal of the plainest
    # version counts as a permanent failure.
    variants = [(ACCOUNT_IDENTITY, images)]
    if ACCOUNT_IDENTITY:
        variants.append((False, images))
    if images:
        variants.append((False, []))

    for i, (identity, files) in enumerate(variants):
        result, status = _send(webhook_url, webhook_env, post, build_payload(post, identity), files)
        if status in (400, 413) and i + 1 < len(variants):
            print(f"  [discord] Discord refused the message ({status}) — retrying a plainer version")
            continue
        return result
    return None
