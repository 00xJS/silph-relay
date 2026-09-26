"""Fetch recent posts from the tracked X accounts.

Sources, tried in order for each account:
  1. FxTwitter's free JSON API — the primary source (no auth, posts seconds old)
  2. Each Nitter instance in NITTER_BASE, via RSS — the fallback

Both report X's own numeric status IDs, so switching source mid-stream can't
cause repeats. Standard library only — see net.py.
"""
import base64
import html
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit

import envfile
import net

envfile.load()

# FEED_SOURCE: auto (FxTwitter, then Nitter) | fx | nitter
FEED_SOURCE = (os.getenv("FEED_SOURCE") or "auto").strip().lower()
if FEED_SOURCE not in ("auto", "fx", "nitter"):
    print(f"[fetcher] Unknown FEED_SOURCE={FEED_SOURCE!r} — using auto")
    FEED_SOURCE = "auto"

# Primary source. FxEmbed documents no hard rate limit ("please be nice") and
# reserves the right to block abusive IPs/UAs; this relay makes ~10 requests/min.
FX_API_BASE = (os.getenv("FX_API_BASE") or "https://api.fxtwitter.com").rstrip("/")

# Fallback: Nitter instances, tried in order. Instances come and go — nitter.net,
# the original fallback, had gone dark by 2026-09 without anyone noticing — so
# this is a list, it's an Actions variable (changing it needs no commit), and
# the watchdog checks it every hour. Both defaults were verified on 2026-09-26
# to serve RSS to a bot. meowing.monster returned exactly FxTwitter's status IDs
# for all six accounts and runs off Cloudflare, outside FxTwitter's failure
# domain; thepixora sits behind Cloudflare.
DEFAULT_NITTER = "https://nitter.meowing.monster,https://shitter.thepixora.com"
NITTER_INSTANCES = [u.strip().rstrip("/") for u in
                    (os.getenv("NITTER_BASE") or DEFAULT_NITTER).split(",") if u.strip()]

FX_TIMEOUT     = 10   # FxTwitter normally answers in well under a second
NITTER_TIMEOUT = 12
IMAGE_TIMEOUT  = 15

# Cap on posts relayed per account per run. Overflow (older entries) is marked
# seen without posting — prevents a newly added account's whole feed history
# from flooding the channel on its first run.
MAX_NEW_POSTS_PER_RUN = int(os.getenv("MAX_NEW_POSTS_PER_RUN") or 5)

# Never relay posts older than this — stale news isn't news. Old-but-unseen
# posts (deep feed history, outage backlogs) are marked seen silently.
MAX_POST_AGE_HOURS = float(os.getenv("MAX_POST_AGE_HOURS") or 24)

# user_id is X's numeric account ID. Fetching by ID skips a handle->ID lookup
# on FxTwitter's side (~120ms) and survives handle renames.
ACCOUNTS = [
    {"handle": "PokemonGoApp",    "user_id": "2839430431",          "display": "@PokemonGoApp",    "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "LeekDuck",        "user_id": "840992778020630531",  "display": "@LeekDuck",        "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "thepokemodgroup", "user_id": "1702466937928732672", "display": "@thepokemodgroup", "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "ScopelyExplore",  "user_id": "849344094681870336",  "display": "@ScopelyExplore",  "webhook_env": "DISCORD_WEBHOOK_URL"},
    {"handle": "pokemonrestocks", "user_id": "1327781541624377344", "display": "@pokemonrestocks", "webhook_env": "DISCORD_WEBHOOK_URL_RESTOCKS"},
    {"handle": "PokemonDealsTCG", "user_id": "1411405148006404096", "display": "@PokemonDealsTCG", "webhook_env": "DISCORD_WEBHOOK_URL_RESTOCKS"},
]

# Accounts worth re-checking within a single run when fast polling is on.
# Restock drops are the only thing here where seconds genuinely matter.
FAST_HANDLES = [h.strip().lower() for h in
                (os.getenv("FAST_HANDLES") or "pokemonrestocks,PokemonDealsTCG").split(",") if h.strip()]
FAST_ACCOUNTS = [a for a in ACCOUNTS if a["handle"].lower() in FAST_HANDLES]

# Tweet URLs vary by source (twitter.com vs x.com, handle casing), so match
# posts on the numeric status ID embedded in them, not the full URL string.
STATUS_NUM_RE = re.compile(r"/status(?:es)?/(\d+)")

SNOWFLAKE_EPOCH_MS = 1288834974657


def status_num(post_id):
    """Extract the numeric tweet ID from a post ID/URL (falls back to the raw string)."""
    m = STATUS_NUM_RE.search(post_id)
    return m.group(1) if m else post_id


def tweet_time(status_id):
    """Unix time a tweet was created, read from its snowflake ID (None if not an ID)."""
    try:
        return ((int(status_id) >> 22) + SNOWFLAKE_EPOCH_MS) / 1000
    except (TypeError, ValueError):
        return None


def newest_first(items, key):
    """Sort by tweet ID descending.

    Tweet IDs are snowflakes, so numeric order IS chronological order. Sources
    do not reliably return sorted data — FxTwitter returns unsorted for most
    accounts — and the per-run cap must keep the NEWEST posts, not whichever
    happened to arrive first.
    """
    def sort_key(item):
        try:
            return int(key(item))
        except (TypeError, ValueError):
            return 0
    return sorted(items, key=sort_key, reverse=True)


# ---------------------------------------------------------------- images

IMAGE_EXTS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
EXT_TYPES  = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "gif": "image/gif", "webp": "image/webp"}


def clean_image_url(url):
    """Convert Twitter query-string image URLs to direct file URLs."""
    match = re.match(r'(https://pbs\.twimg\.com/media/[^?&\s]+)\?format=(\w+)', url)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return url


def download_image(url):
    """Fetch one image into memory. Returns (bytes, content_type) or None."""
    try:
        r = net.request(url, timeout=IMAGE_TIMEOUT)
        if not r.ok:
            raise OSError(f"HTTP {r.status}")
    except Exception as e:
        print(f"    [fetcher] Image download failed ({url}): {e}")
        return None
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in IMAGE_EXTS:
        ctype = EXT_TYPES.get(urlsplit(url).path.rsplit(".", 1)[-1].lower(), "image/jpeg")
    return r.body, ctype


def download_images(urls):
    """Download images concurrently, keeping their order.

    Returns [(filename, bytes, content_type)] for the ones that arrived.
    """
    urls = list(dict.fromkeys(u for u in urls if u))  # de-duplicated, order kept
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(len(urls), 4)) as pool:
        results = list(pool.map(download_image, urls))
    images = []
    for got in results:
        if got:
            data, ctype = got
            images.append((f"image-{len(images) + 1}.{IMAGE_EXTS[ctype]}", data, ctype))
    return images


# ---------------------------------------------------------------- FxTwitter

def fx_avatar(statuses, account):
    """The tracked account's own avatar, taken from any post it wrote."""
    for st in statuses:
        author = st.get("author") or {}
        mine = (str(author.get("id") or "") == account.get("user_id")
                or (author.get("screen_name") or "").lower() == account["handle"].lower())
        if mine and author.get("avatar_url"):
            return author["avatar_url"]
    return None


def fetch_account_fx(account, seen_nums):
    """Fetch recent posts via FxTwitter's JSON API.

    Returns (posts, skip_ids), or None on failure so the caller can fall back.
    """
    handle = account["handle"]
    target = f"id:{account['user_id']}" if account.get("user_id") else handle
    url    = f"{FX_API_BASE}/2/profile/{target}/statuses"

    print(f"  [fetcher] Fetching {account['display']} — {url}")
    try:
        r = net.request(url, timeout=FX_TIMEOUT)
        data = r.json() if r.ok else {}
    except Exception as e:
        print(f"  [fetcher] FxTwitter request failed for {handle}: {e}")
        return None

    if not isinstance(data, dict):
        data = {}
    statuses = data.get("results")
    if data.get("code") != 200 or not isinstance(statuses, list) or not statuses:
        print(f"  [fetcher] FxTwitter unusable response for {handle} "
              f"(HTTP {r.status}, code={data.get('code')})")
        return None

    avatar = fx_avatar(statuses, account)
    posts, skip_ids = [], set()
    skipped = 0
    listed = set()  # FxTwitter can list one post twice (pinned, re-shared) — handle it once
    age_cutoff = time.time() - MAX_POST_AGE_HOURS * 3600
    for st in newest_first(statuses, lambda s: s.get("id")):
        num = str(st.get("id") or "").strip()
        if not num or num in listed:
            continue
        listed.add(num)
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

        # On a repost the URL/author belong to the ORIGINAL poster, so record
        # who actually wrote it — the relaying account is `account`.
        author = ((st.get("author") or {}).get("screen_name") or "").strip()

        posts.append({
            "id":        post_url,
            "account":   account,
            "author":    author,
            "avatar":    avatar,
            "text":      (st.get("text") or "").strip(),
            "url":       post_url,
            "published": st.get("created_at", ""),
            "images":    download_images(clean_image_url(u) for u in photo_urls),
        })

    summary = f"  [fetcher] {account['display']} via FxTwitter: {len(posts)} new, {skipped} already seen"
    if skip_ids:
        summary += f", {len(skip_ids)} marked seen unposted"
    print(summary)
    return posts, skip_ids


# ---------------------------------------------------------------- Nitter

DC_CREATOR    = "{http://purl.org/dc/elements/1.1/}creator"
NITTER_PIC_RE = re.compile(r"/pic/(?:orig/)?(.+)$")
LINK_RE       = re.compile(r'<a\s[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
IMG_RE        = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
QUOTE_RE      = re.compile(r"<hr\b|<blockquote\b", re.I)

# Instances that failed at the network level earlier in this run — not retried
# again until the next run, so a dead one can't cost a timeout on every poll.
_dead_instances = set()


def nitter_image_url(url):
    """Rewrite a Nitter-proxied image URL back to Twitter's own CDN.

    Instances differ: /pic/media%2FX.jpg, /pic/orig/media%2FX.jpg,
    /pic/pbs.twimg.com%2Fmedia%2FX.jpg, or /pic/enc/<base64 of the URL>.
    """
    m = NITTER_PIC_RE.search(url)
    if not m:
        return url
    target = m.group(1)
    if target.startswith("enc/"):
        try:
            encoded = target[4:]
            target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except Exception:
            return url
    target = unquote(target).lstrip("/")
    if target.startswith(("http://", "https://")):
        return target
    if target.startswith("pbs.twimg.com/"):
        return "https://" + target
    return "https://pbs.twimg.com/" + target


def _link_text(match, instance_host):
    href  = html.unescape(match.group(1))
    label = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
    if label.startswith(("#", "@")):  # hashtags and mentions: keep what the tweet showed
        return label
    parts = urlsplit(href)
    if parts.hostname and parts.hostname == instance_host:  # a link back into Nitter
        return "https://x.com" + parts.path
    return href if href.startswith(("http://", "https://")) else label


def nitter_text(description, instance_host):
    """A Nitter item's HTML -> the tweet text as FxTwitter would give it: line
    breaks kept, links expanded to full URLs, any quoted tweet left out."""
    own = QUOTE_RE.split(description, maxsplit=1)[0]
    s = re.sub(r"\s+", " ", own)  # source whitespace means nothing in HTML
    s = LINK_RE.sub(lambda m: _link_text(m, instance_host), s)
    s = re.sub(r"<br\s*/?>|</p\s*>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def nitter_images(description):
    """The post's own images — not the quoted tweet's — as pbs.twimg.com URLs."""
    own = QUOTE_RE.split(description, maxsplit=1)[0]
    return [nitter_image_url(html.unescape(u)) for u in IMG_RE.findall(own)]


def _rss_time(text):
    try:
        return parsedate_to_datetime(text).timestamp()
    except Exception:
        return None


def _fetch_nitter_instance(base, account, seen_nums):
    """Posts from one Nitter instance: (posts, skip_ids), or None if it didn't answer."""
    handle = account["handle"]
    host   = urlsplit(base).hostname or base
    url    = f"{base}/{handle}/rss"

    print(f"  [fetcher] Fetching {account['display']} — {url}")
    try:
        r = net.request(url, timeout=NITTER_TIMEOUT)
    except Exception as e:
        print(f"  [fetcher] Nitter ({host}) request failed for {handle}: {e}")
        _dead_instances.add(base)
        return None
    if not r.ok:
        print(f"  [fetcher] Nitter ({host}) returned HTTP {r.status} for {handle}")
        if r.status != 404:  # 404 is about this account; anything else is the instance
            _dead_instances.add(base)
        return None

    try:
        channel = ET.fromstring(r.body).find("channel")
    except ET.ParseError as e:  # usually a bot-challenge page instead of RSS
        print(f"  [fetcher] Nitter ({host}) sent something other than RSS for {handle}: {e}")
        _dead_instances.add(base)
        return None
    items = channel.findall("item") if channel is not None else []
    if not items:
        print(f"  [fetcher] Nitter ({host}) returned no entries for {handle}")
        return None

    avatar = nitter_image_url(channel.findtext("image/url") or "") or None

    # Pair each entry with its numeric status ID up front, so we can order by it
    entries = []
    for item in items:
        guid = (item.findtext("guid") or "").strip()
        if guid.isdigit():
            entries.append((guid, item))
            continue
        m = STATUS_NUM_RE.search(guid) or STATUS_NUM_RE.search(item.findtext("link") or "")
        if m:
            entries.append((m.group(1), item))

    posts, skip_ids = [], set()
    skipped = 0
    listed = set()
    age_cutoff = time.time() - MAX_POST_AGE_HOURS * 3600

    for num, item in newest_first(entries, lambda pair: pair[0]):
        if num in listed:
            continue
        listed.add(num)
        if num in seen_nums:
            skipped += 1
            continue

        # The link path names the ORIGINAL author, which differs on a repost
        link_user = urlsplit(item.findtext("link") or "").path.strip("/").split("/")[0]
        author    = link_user or (item.findtext(DC_CREATOR) or "").lstrip("@").strip()
        post_url  = f"https://x.com/{author or handle}/status/{num}"

        ts = _rss_time(item.findtext("pubDate") or "")
        if ts and ts < age_cutoff:
            skip_ids.add(post_url)
            continue

        # Nitter titles replies to other people "R to @someone:" — skip those,
        # keep self-replies (thread continuations)
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        reply_to = re.match(r"R to @(\w+):", title)
        if reply_to and reply_to.group(1).lower() != handle.lower():
            skip_ids.add(post_url)
            continue

        if len(posts) >= MAX_NEW_POSTS_PER_RUN:
            skip_ids.add(post_url)
            continue

        description = item.findtext("description") or ""
        text = nitter_text(description, host) or re.sub(r"^(?:RT by|R to) @\w+:\s*", "", title)

        posts.append({
            "id":        post_url,
            "account":   account,
            "author":    author,
            "avatar":    avatar,
            "text":      text,
            "url":       post_url,
            "published": item.findtext("pubDate") or "",
            "images":    download_images(nitter_images(description)),
        })

    summary = f"  [fetcher] {account['display']} via Nitter ({host}): {len(posts)} new, {skipped} already seen"
    if skip_ids:
        summary += f", {len(skip_ids)} marked seen unposted"
    print(summary)
    return posts, skip_ids


def fetch_account_nitter(account, seen_nums):
    """Try each live Nitter instance in turn. Returns (posts, skip_ids, host) or None."""
    for base in NITTER_INSTANCES:
        if base in _dead_instances:
            continue
        result = _fetch_nitter_instance(base, account, seen_nums)
        if result is not None:
            return result[0], result[1], urlsplit(base).hostname or base
    return None


# ---------------------------------------------------------------- orchestration

# Accounts FxTwitter failed for earlier in this run while Nitter covered them.
# Later polls go straight to Nitter instead of waiting out another timeout.
_fx_down = set()


def fetch_account(account, seen_nums):
    """Fetch one account, falling through sources until one answers.

    Returns (posts, skip_ids, source): source names what answered ("fxtwitter"
    or a Nitter host), or is None when nothing did.
    """
    handle = account["handle"]
    fx_failed = False

    if FEED_SOURCE in ("auto", "fx") and handle not in _fx_down:
        result = fetch_account_fx(account, seen_nums)
        if result is not None:
            return result[0], result[1], "fxtwitter"
        if FEED_SOURCE == "fx":
            return [], set(), None
        fx_failed = True
        print(f"  [fetcher] FxTwitter unavailable for {handle} — trying Nitter")

    if FEED_SOURCE in ("auto", "nitter"):
        result = fetch_account_nitter(account, seen_nums)
        if result is not None:
            if fx_failed:
                _fx_down.add(handle)
            return result
        print(f"  [fetcher] No source answered for {handle}")

    return [], set(), None


def fetch_all(seen_ids, accounts=None):
    """Fetch the given accounts (default: all) in parallel.

    Returns (posts, skip_ids, sources): the posts to publish, the entries to
    mark seen without posting, and {handle: source that answered, or None}.
    """
    accounts = ACCOUNTS if accounts is None else accounts
    if not accounts:
        return [], set(), {}

    seen_nums = {status_num(i) for i in seen_ids}
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        results = list(pool.map(lambda acc: fetch_account(acc, seen_nums), accounts))

    # Two tracked accounts can surface the same tweet in one poll (one reposts
    # the other). Relay it once — under the account that wrote it, if tracked.
    candidates = [(acc, post) for acc, (posts, _, _) in zip(accounts, results) for post in posts]
    candidates.sort(key=lambda ap: (ap[1].get("author") or "").lower() != ap[0]["handle"].lower())
    posts, taken = [], set()
    for _, post in candidates:
        num = status_num(post["id"])
        if num not in taken:
            taken.add(num)
            posts.append(post)

    # Never mark seen-unposted a tweet another account is posting this poll
    skip_ids = {s for _, skips, _ in results for s in skips if status_num(s) not in taken}
    sources  = {acc["handle"]: source for acc, (_, _, source) in zip(accounts, results)}
    return posts, skip_ids, sources
