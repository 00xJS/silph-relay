"""Tweet source: RSSHub twitter routes, sped up with a combined-search fast path.

RSSHub talks to X's internal endpoints using ONE burner account's
TWITTER_AUTH_TOKEN cookie (set on the RSSHub side). Two detection paths run on
independent upstream rate-limit budgets:

  * Fast path — the /twitter/keyword route with a combined
    "from:A OR from:B ..." query returns the latest tweets across every account
    in a single upstream search call, so it can poll sub-minute on one token.
  * Backstop — the per-account /twitter/user routes run every Nth cycle to
    catch tweets X's search index silently filters (a real risk for the
    affiliate-link deals account). Separate upstream budget from search.

Post dict shape (unchanged from the original pipeline):
    {id, account, text, url, published, images: [(path, filename)], webhook_url}
IDs are canonical numeric tweet IDs so search and user entries dedupe cleanly.
"""

import re
import tempfile
import time
from urllib.parse import quote

import feedparser
import requests
from dotenv import load_dotenv

from config import (
    ACCOUNTS,
    RSSHUB_URL,
    SWEEP_INTERVAL,
    SWEEP_INTERVAL_DEGRADED,
    webhook_for,
)

load_dotenv()

_handle_map = {a["handle"].lower(): a for a in ACCOUNTS}
_search_fails = 0  # consecutive search-feed failures (2+ → degraded mode)
_last_sweep = 0.0  # timestamp of the last per-account sweep


def _canonical_id(raw):
    """Normalize an RSS entry ID/link to the numeric tweet ID."""
    s = str(raw)
    m = re.search(r"/status/(\d+)", s)
    if m:
        return m.group(1)
    return s if s.isdigit() else s.strip()


def _account_for_link(link):
    """Resolve which tracked account a tweet link belongs to, or None."""
    m = re.search(r"(?:twitter|x)\.com/([^/]+)/status/", link or "")
    if not m:
        return None
    return _handle_map.get(m.group(1).lower())


def clean_image_url(url):
    """Convert Twitter query-string image URLs to direct file URLs."""
    match = re.match(r"(https://pbs\.twimg\.com/media/[^?&\s]+)\?format=(\w+)", url)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return url


def extract_image_urls(description):
    """Extract and deduplicate cleaned image URLs from RSS description HTML."""
    found = []
    found.extend(
        re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', description, re.IGNORECASE)
    )
    found.extend(re.findall(r"https://pbs\.twimg\.com/media/[^\s\"'<>&]+", description))

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


def setup():
    """Warm-up ping so RSSHub is ready before the first fetch."""
    if not RSSHUB_URL:
        print("  [fetcher] RSSHUB_URL not set")
        return
    try:
        requests.get(RSSHUB_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        print("  [fetcher] RSSHub warmed up")
    except Exception:
        print("  [fetcher] Warm-up ping failed — continuing anyway")


# ── feed fetching ─────────────────────────────────────────────────────────────
def _search_url():
    clauses = " OR ".join(f"from:{a['handle']}" for a in ACCOUNTS)
    return f"{RSSHUB_URL}/twitter/keyword/{quote(clauses, safe='')}"


def _user_url(account):
    return f"{RSSHUB_URL}/twitter/user/{account['handle']}"


def _fetch_feed(url, label):
    """GET one RSSHub feed.

    Returns feedparser entries ([] for an empty feed), or None when the request
    itself failed — callers use None to detect a broken route.
    """
    try:
        r = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45, allow_redirects=True
        )
        r.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"  [fetcher] Timeout fetching {label}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"  [fetcher] Request failed for {label}: {e}")
        return None

    feed = feedparser.parse(r.content)
    if not feed.entries:
        print(f"  [fetcher] No entries for {label}")
    return feed.entries


def _parse_entries(entries, seen_ids, picked, download=True):
    """Convert feed entries to post dicts, skipping seen/duplicate/foreign ones."""
    posts = []
    for entry in entries:
        link = entry.get("link", "")
        post_id = _canonical_id(entry.get("id", link))
        if not post_id or post_id in seen_ids or post_id in picked:
            continue

        account = _account_for_link(link)
        if account is None:
            continue  # search feed can only contain our accounts, but be safe

        description = entry.get("summary", entry.get("description", ""))
        plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", description)).strip()

        local_images = []
        if download:
            for img_url in extract_image_urls(description):
                path, name = download_image(img_url)
                if path:
                    local_images.append((path, name))

        picked.add(post_id)
        posts.append(
            {
                "id": post_id,
                "account": account,
                "text": plain_text,
                "url": link,
                "published": entry.get("published", ""),
                "images": local_images,
                "webhook_url": webhook_for(account),
                "_ts": entry.get("published_parsed"),
            }
        )
    return posts


def _collect(seen_ids, include_sweep, download=True):
    """Fetch the fast search feed, plus per-account feeds when sweeping."""
    global _search_fails
    picked = set()

    entries = _fetch_feed(_search_url(), "combined search")
    if entries is None:
        _search_fails += 1
        if _search_fails == 2:
            print("  [fetcher] search feed failing — degrading to fast sweeps "
                  f"every {SWEEP_INTERVAL_DEGRADED}s until it recovers")
        posts = []
    else:
        if _search_fails >= 2:
            print("  [fetcher] search feed recovered — back to sub-minute mode")
        _search_fails = 0
        posts = _parse_entries(entries, seen_ids, picked, download)
    search_new = len(posts)

    if include_sweep:
        for account in ACCOUNTS:
            found = _fetch_feed(_user_url(account), account["display"])
            posts.extend(_parse_entries(found or [], seen_ids, picked, download))

    sweep_new = len(posts) - search_new
    label = f"search: {search_new} new"
    if include_sweep:
        label += f", sweep: {sweep_new} more"
    print(f"  [fetcher] {label}")

    # Oldest first so Discord reads chronologically
    posts.sort(key=lambda p: (p["_ts"] is None, time.mktime(p["_ts"]) if p["_ts"] else 0))
    for p in posts:
        p.pop("_ts", None)
    return posts


def fetch_all(seen_ids):
    """Fetch new posts.

    Healthy: fast combined-search every call + per-account sweep every
    SWEEP_INTERVAL. Degraded (search route down): sweeps speed up to
    SWEEP_INTERVAL_DEGRADED so latency falls to ~2 min instead of dying.
    """
    global _last_sweep
    interval = SWEEP_INTERVAL_DEGRADED if _search_fails >= 2 else SWEEP_INTERVAL
    now = time.time()
    include_sweep = (now - _last_sweep) >= interval
    if include_sweep:
        _last_sweep = now
    return _collect(seen_ids, include_sweep=include_sweep, download=True)


def current_ids():
    """IDs of all currently visible tweets (both paths), without image downloads.
    Used to prime a fresh install so it doesn't repost history."""
    return {p["id"] for p in _collect(set(), include_sweep=True, download=False)}
