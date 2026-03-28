import feedparser
import requests
import re
import os
import json
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

RSSHUB_URL = os.getenv("RSSHUB_URL", "").rstrip("/")

ACCOUNTS = [
    {"handle": "PokemonGoApp",    "display": "@PokemonGoApp",    "color": 0xEE1515},
    {"handle": "LeekDuck",        "display": "@LeekDuck",        "color": 0x5B8C3E},
    {"handle": "thepokemodgroup", "display": "@thepokemodgroup", "color": 0x5865F2},
]


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


def fetch_account(account, seen_ids):
    """Fetch new posts for a single account. Returns list of post dicts."""
    handle  = account["handle"]
    url     = f"{RSSHUB_URL}/twitter/user/{handle}"
    posts   = []

    print(f"  [fetcher] Fetching {account['display']} — {url}")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45, allow_redirects=True)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"  [fetcher] Timeout fetching {handle}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"  [fetcher] Request failed for {handle}: {e}")
        return []

    feed = feedparser.parse(r.content)
    if not feed.entries:
        print(f"  [fetcher] No entries for {handle}")
        return []

    skipped = 0
    for entry in feed.entries:
        post_id = entry.get("id", entry.get("link", "")).strip()
        if not post_id or post_id in seen_ids:
            skipped += 1
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

    new_count = len(posts)
    print(f"  [fetcher] {account['display']}: {new_count} new, {skipped} already seen")
    return posts


def fetch_all(seen_ids):
    """Fetch new posts from all tracked accounts."""
    all_posts = []
    for account in ACCOUNTS:
        all_posts.extend(fetch_account(account, seen_ids))
    return all_posts
