import feedparser
import requests
import re
import os
import json
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

RSSHUB_URL        = os.getenv("RSSHUB_URL", "").rstrip("/")
RSSHUB_ACCESS_KEY = os.getenv("RSSHUB_ACCESS_KEY", "")

# Primary tweet source: FxTwitter's free JSON API — no auth required,
# documented limit 1000 req/min per IP (this pipeline uses ~2.5/min).
# RSSHub is the automatic fallback when it fails.
# FEED_SOURCE: auto (FxTwitter, fall back to RSSHub) | fx | rsshub
FX_API_BASE = os.getenv("FX_API_BASE", "https://api.fxtwitter.com").rstrip("/")
FEED_SOURCE = os.getenv("FEED_SOURCE", "auto").lower()

# Server-side feed size cap for the RSSHub path — smaller responses
FEED_LIMIT = int(os.getenv("FEED_LIMIT", "5"))

# Cap on posts relayed per account per run. Overflow (older entries) is marked
# seen without posting — prevents a newly added account's whole feed history
# from flooding the channel on its first run.
MAX_NEW_POSTS_PER_RUN = int(os.getenv("MAX_NEW_POSTS_PER_RUN", "5"))

# Never relay posts older than this — stale news isn't news. Old-but-unseen
# posts (deep feed history, outage backlogs) are marked seen silently.
MAX_POST_AGE_HOURS = float(os.getenv("MAX_POST_AGE_HOURS", "24"))

ACCOUNTS = [
    {"handle": "PokemonGoApp",    "display": "@PokemonGoApp",    "color": 0xEE1515, "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "LeekDuck",        "display": "@LeekDuck",        "color": 0x5B8C3E, "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "thepokemodgroup", "display": "@thepokemodgroup", "color": 0x5865F2, "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "ScopelyExplore",  "display": "@ScopelyExplore",  "color": 0x8E44AD, "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "pokemonrestocks", "display": "@pokemonrestocks", "color": 0xFF6B35, "webhook_env": "DISCORD_WEBHOOK_URL_RESTOCKS"},
    {"handle": "PokemonDealsTCG", "display": "@PokemonDealsTCG", "color": 0x3B4CCA, "webhook_env": "DISCORD_WEBHOOK_URL_RESTOCKS"},
]

# Tweet URLs vary by source (twitter.com vs x.com, handle casing), so dedupe
# on the numeric status ID embedded in them, not the full URL string.
STATUS_NUM_RE = re.compile(r"/status(?:es)?/(\d+)")


def status_num(post_id):
    """Extract the numeric tweet ID from a post ID/URL (falls back to the raw string)."""
    m = STATUS_NUM_RE.search(post_id)
    return m.group(1) if m else post_id


def clean_image_url(url):
    """Convert Twitter query-string image URLs to direct file URLs."""
    match = re.match(r'(https://pbs\.twimg\.com/media/[^?&\s]+)\?format=(\w+)', url)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return url


def extract_image_urls(description):
    """Extract and deduplicate cleaned image URLs from RSS description HTML."""
    found = []
    found.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', description, re.IGNORECASE))
    found.extend(re.findall(r'https://pbs\.twimg\.com/media/[^\s"\'<>&]+', description))

    seen, result = set(), []
    for url in found:
        clean = clean_image_url(url.strip())
        if clean not in seen and "pbs.twimg.com" in clean:
            seen.add(clean)
            result.append(clean)
    return result


def download_image(url):
    """Download an image to a temp file. Returns (path, filename) or (None, None)."""
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()

        ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
        tmp.write(r.content)
        tmp.close()
        return tmp.name, f"image.{ext}"
    except Exception as e:
        print(f"    [fetcher] Image download failed ({url}): {e}")
        return None, None


def warmup():
    """Ping RSSHub so a sleeping host wakes up before we start fetching."""
    try:
        requests.get(RSSHUB_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        print("  [fetcher] RSSHub warmed up")
    except Exception:
        print("  [fetcher] Warm-up ping failed — continuing anyway")


def fetch_account_fx(account, seen_nums):
    """Fetch recent posts via FxTwitter's JSON API.

    Returns (posts, skip_ids), or None on failure so the caller can fall back.
    """
    handle = account["handle"]
    url    = f"{FX_API_BASE}/2/profile/{handle}/statuses"

    print(f"  [fetcher] Fetching {account['display']} — {url}")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [fetcher] FxTwitter request failed for {handle}: {e}")
        return None

    statuses = data.get("results")
    if data.get("code") != 200 or not isinstance(statuses, list) or not statuses:
        print(f"  [fetcher] FxTwitter unusable response for {handle} (code={data.get('code')})")
        return None

    posts, skip_ids = [], set()
    skipped = 0
    age_cutoff = time.time() - MAX_POST_AGE_HOURS * 3600
    for st in statuses:  # newest-first
        num = str(st.get("id") or "").strip()
        if not num:
            continue
        if num in seen_nums:
            skipped += 1
            continue

        post_url = st.get("url") or f"https://twitter.com/{handle}/status/{num}"

        # Too old to be worth relaying (deep history / pinned posts) — mark seen
        ts = st.get("created_timestamp")
        if ts and ts < age_cutoff:
            skip_ids.add(post_url)
            continue

        # Skip replies to OTHER accounts; keep self-replies (thread continuations)
        replying_to = st.get("replying_to")
        if isinstance(replying_to, dict):
            replying_to = replying_to.get("screen_name") or ""
        if replying_to and str(replying_to).lower() != handle.lower():
            skip_ids.add(post_url)
            continue

        # Cap posts per run — overflow is older backlog, mark seen unposted
        if len(posts) >= MAX_NEW_POSTS_PER_RUN:
            skip_ids.add(post_url)
            continue

        media      = st.get("media") or {}
        photo_urls = [p.get("url") for p in (media.get("photos") or []) if p.get("url")]

        local_images = []
        for img_url in photo_urls:
            path, name = download_image(clean_image_url(img_url))
            if path:
                local_images.append((path, name))

        posts.append({
            "id":        post_url,
            "account":   account,
            "text":      (st.get("text") or "").strip(),
            "url":       post_url,
            "published": st.get("created_at", ""),
            "images":    local_images,  # list of (tmp_path, filename)
        })

    summary = f"  [fetcher] {account['display']} via FxTwitter: {len(posts)} new, {skipped} already seen"
    if skip_ids:
        summary += f", {len(skip_ids)} marked seen unposted"
    print(summary)
    return posts, skip_ids


def fetch_account_rsshub(account, seen_ids, seen_nums):
    """Fetch new posts via an RSSHub feed. Returns (posts, skip_ids)."""
    handle   = account["handle"]
    url      = f"{RSSHUB_URL}/twitter/user/{handle}?limit={FEED_LIMIT}"
    posts    = []
    skip_ids = set()

    # Don't log the full URL — it may carry the access key
    print(f"  [fetcher] Fetching {account['display']} — {RSSHUB_URL}/twitter/user/{handle}")
    if RSSHUB_ACCESS_KEY:
        url += f"&key={RSSHUB_ACCESS_KEY}"

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45, allow_redirects=True)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"  [fetcher] Timeout fetching {handle}")
        return [], set()
    except requests.exceptions.RequestException as e:
        print(f"  [fetcher] Request failed for {handle}: {e}")
        return [], set()

    feed = feedparser.parse(r.content)
    if not feed.entries:
        print(f"  [fetcher] No entries for {handle}")
        return [], set()

    skipped = 0
    for entry in feed.entries:
        post_id = entry.get("id", entry.get("link", "")).strip()
        if not post_id or post_id in seen_ids or status_num(post_id) in seen_nums:
            skipped += 1
            continue

        # Entries are newest-first, so overflow beyond the cap is the older
        # backlog — mark it seen without posting.
        if len(posts) >= MAX_NEW_POSTS_PER_RUN:
            skip_ids.add(post_id)
            continue

        description = entry.get("summary", entry.get("description", ""))
        plain_text  = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', description)).strip()
        image_urls  = extract_image_urls(description)

        # Download images to temp files
        local_images = []
        for img_url in image_urls:
            path, name = download_image(img_url)
            if path:
                local_images.append((path, name))

        posts.append({
            "id":        post_id,
            "account":   account,
            "text":      plain_text,
            "url":       entry.get("link", ""),
            "published": entry.get("published", ""),
            "images":    local_images,  # list of (tmp_path, filename)
        })

    summary = f"  [fetcher] {account['display']} via RSSHub: {len(posts)} new, {skipped} already seen"
    if skip_ids:
        summary += f", {len(skip_ids)} older backlog marked seen"
    print(summary)
    return posts, skip_ids


def fetch_account(account, seen_ids, seen_nums):
    """Fetch one account from the configured source, with automatic fallback."""
    if FEED_SOURCE != "rsshub":
        result = fetch_account_fx(account, seen_nums)
        if result is not None:
            return result
        if FEED_SOURCE == "fx":
            return [], set()
        print(f"  [fetcher] Falling back to RSSHub for {account['handle']}")
    return fetch_account_rsshub(account, seen_ids, seen_nums)


def fetch_all(seen_ids):
    """Fetch new posts from all tracked accounts in parallel.

    Returns (posts, skip_ids) — skip_ids are entries to mark seen
    without posting.
    """
    seen_nums = {status_num(i) for i in seen_ids}

    if FEED_SOURCE == "rsshub" and RSSHUB_URL:
        warmup()

    all_posts, skip_ids = [], set()
    with ThreadPoolExecutor(max_workers=len(ACCOUNTS)) as pool:
        for posts, skips in pool.map(lambda acc: fetch_account(acc, seen_ids, seen_nums), ACCOUNTS):
            all_posts.extend(posts)
            skip_ids |= skips
    return all_posts, skip_ids
