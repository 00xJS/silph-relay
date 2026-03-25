import feedparser
import requests
import re
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

RSSHUB_URL = os.getenv("RSSHUB_URL")
IMAGES_DIR = Path("data/images")


# -------------------------------------------------------------------
# clean_image_url
# Converts Twitter's query-string image URLs to direct file URLs.
# Example:
#   IN:  https://pbs.twimg.com/media/ABC123?format=jpg&name=orig
#   OUT: https://pbs.twimg.com/media/ABC123.jpg
# -------------------------------------------------------------------
def clean_image_url(url):
    match = re.match(r'(https://pbs\.twimg\.com/media/[^?&\s]+)\?format=(\w+)', url)
    if match:
        base = match.group(1)
        ext  = match.group(2)
        return f"{base}.{ext}"
    return url


# -------------------------------------------------------------------
# extract_images_from_description
# The RSS description field contains raw HTML. This pulls out all
# image URLs from <img> tags and bare twimg.com URLs, then cleans them.
# -------------------------------------------------------------------
def extract_images_from_description(description):
    found = []

    # Extract from <img src="..."> tags
    img_tags = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', description, re.IGNORECASE)
    found.extend(img_tags)

    # Extract bare pbs.twimg.com URLs not already caught above
    bare_urls = re.findall(r'https://pbs\.twimg\.com/media/[^\s"\'<>&]+', description)
    found.extend(bare_urls)

    # Clean and deduplicate while preserving order
    seen = set()
    cleaned = []
    for url in found:
        clean = clean_image_url(url.strip())
        if clean not in seen and 'pbs.twimg.com' in clean:
            seen.add(clean)
            cleaned.append(clean)

    return cleaned


# -------------------------------------------------------------------
# download_image
# Downloads an image from a URL and saves it to data/images/.
# Returns the local file path, or None if the download fails.
# -------------------------------------------------------------------
def download_image(url, filename):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        # Get extension from the cleaned URL (e.g. .jpg)
        ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"

        path = IMAGES_DIR / f"{filename}.{ext}"
        with open(path, "wb") as f:
            f.write(response.content)
        return str(path)

    except Exception as e:
        print(f"  [fetcher] Image download failed ({url}): {e}")
        return None


# -------------------------------------------------------------------
# fetch_posts
# Fetches feed content via requests (handles Render cold starts and
# redirects), then parses with feedparser.
# Returns a list of post dicts: id, text, url, published, images
# -------------------------------------------------------------------
def fetch_posts():
    feed_url = f"{RSSHUB_URL}/twitter/user/PokemonGoApp"
    print(f"  [fetcher] Fetching: {feed_url}")

    try:
        # Use requests to fetch raw content so we can control timeout
        # and headers. Render's free tier sleeps after inactivity so
        # we give it up to 45 seconds to wake up and respond.
        response = requests.get(
            feed_url,
            timeout=45,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print("  [fetcher] Request timed out — Render may still be waking up. Try again in 30 seconds.")
        return []
    except requests.exceptions.RequestException as e:
        print(f"  [fetcher] Request failed: {e}")
        return []

    # Pass raw content to feedparser instead of a URL
    feed = feedparser.parse(response.content)

    if not feed.entries:
        print("  [fetcher] Feed returned no entries — check cookie or feed URL")
        return []

    posts = []

    for entry in feed.entries:
        post_id = entry.get("id", entry.get("link", "")).strip()

        # Use description as primary text source — contains full tweet
        # content and embedded image HTML
        description = entry.get("summary", entry.get("description", ""))

        # Strip HTML tags to get clean plain text for the parser
        plain_text = re.sub(r'<[^>]+>', ' ', description).strip()
        plain_text = re.sub(r'\s+', ' ', plain_text)

        # Extract and download images from description HTML
        image_urls   = extract_images_from_description(description)
        local_images = []

        for i, img_url in enumerate(image_urls):
            safe_id = re.sub(r'[^\w]', '_', post_id)[-50:]
            path = download_image(img_url, f"{safe_id}_{i}")
            if path:
                local_images.append(path)

        posts.append({
            "id":        post_id,
            "text":      plain_text,
            "url":       entry.get("link", ""),
            "published": entry.get("published", ""),
            "images":    local_images,
        })

    print(f"  [fetcher] Retrieved {len(posts)} posts")
    return posts
