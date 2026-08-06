import json
from pathlib import Path

SEEN_IDS_FILE = Path("data/seen_ids.json")

# When each post reached Discord: [[status_id, unix_seconds], ...].
# The tweet's own creation time is encoded in its ID, so this is all the
# dashboard needs to derive delivery latency.
DELIVERIES_FILE = Path("data/deliveries.json")
MAX_DELIVERIES  = 1000

# A short log of what was actually relayed, for the dashboard's post table.
# Also the authoritative record of WHICH tracked account surfaced each post —
# on a repost the tweet URL names the original author, not the relaying account.
RECENT_POSTS_FILE = Path("data/recent_posts.json")
MAX_RECENT_POSTS  = 200
SNIPPET_CHARS     = 220


def load_seen_ids():
    """Load previously processed post IDs from disk."""
    if SEEN_IDS_FILE.exists():
        with open(SEEN_IDS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    """Persist seen post IDs to disk."""
    SEEN_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(sorted(list(ids)), f, indent=2)


def load_deliveries():
    """Load [status_id, unix_seconds] delivery records. Never raises."""
    if not DELIVERIES_FILE.exists():
        return []
    try:
        with open(DELIVERIES_FILE) as f:
            data = json.load(f)
        return [(str(r[0]), int(r[1])) for r in data if isinstance(r, (list, tuple)) and len(r) == 2]
    except Exception as e:
        print(f"  [tracker] Couldn't read {DELIVERIES_FILE}: {e}")
        return []


def load_recent_posts():
    """Load the relayed-post log. Never raises."""
    if not RECENT_POSTS_FILE.exists():
        return []
    try:
        with open(RECENT_POSTS_FILE) as f:
            data = json.load(f)
        return [r for r in data if isinstance(r, dict) and r.get("id")]
    except Exception as e:
        print(f"  [tracker] Couldn't read {RECENT_POSTS_FILE}: {e}")
        return []


def save_recent_posts(records):
    """Persist the newest MAX_RECENT_POSTS relayed posts, newest last."""
    newest = {}
    for r in records:
        rid = str(r.get("id", ""))
        if rid:
            newest[rid] = r
    trimmed = sorted(newest.values(), key=lambda r: r.get("delivered", 0))[-MAX_RECENT_POSTS:]
    RECENT_POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RECENT_POSTS_FILE, "w") as f:
        json.dump(trimmed, f, separators=(",", ":"), ensure_ascii=False)


def make_post_record(post, status_id, delivered_at):
    """Build one log entry for a successfully relayed post."""
    text = " ".join((post.get("text") or "").split())
    if len(text) > SNIPPET_CHARS:
        text = text[: SNIPPET_CHARS - 1].rstrip() + "…"
    record = {
        "id":        str(status_id),
        "acct":      post["account"]["handle"],
        "text":      text,
        "url":       post.get("url", ""),
        "delivered": int(delivered_at),
    }
    if post.get("images"):
        record["img"] = len(post["images"])
    # Only meaningful when it differs — i.e. the tracked account reposted it
    author = (post.get("author") or "").strip()
    if author and author.lower() != post["account"]["handle"].lower():
        record["author"] = author
    return record


def save_deliveries(records):
    """Persist delivery records, de-duplicated and capped to the newest MAX."""
    newest = {}
    for sid, ts in records:
        if sid not in newest or ts < newest[sid]:
            newest[sid] = int(ts)
    trimmed = sorted(newest.items(), key=lambda r: r[1])[-MAX_DELIVERIES:]
    DELIVERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DELIVERIES_FILE, "w") as f:
        json.dump([[s, t] for s, t in trimmed], f, separators=(",", ":"))
