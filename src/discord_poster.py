import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Max images Discord accepts per message
MAX_IMAGES = 10


def post_to_discord(post, webhook_url=None):
    """
    Post a tweet to Discord via webhook.
    Sends text as message content and attaches images as files.
    Returns True on success, False on failure.

    webhook_url: optional override; falls back to post["webhook_url"] or
    DISCORD_WEBHOOK_URL env for back-compat.
    """
    account = post["account"]
    text = post["text"] or ""
    url = post["url"]
    images = post.get("images", [])  # list of (tmp_path, filename)

    webhook = (
        webhook_url
        or post.get("webhook_url")
        or os.getenv("DISCORD_WEBHOOK_UPDATES")
        or os.getenv("DISCORD_WEBHOOK_URL", "")
    )
    if not webhook:
        print("  [discord] No webhook URL configured — skipping")
        return False

    # Build message content
    content = f"**{account['display']}**"
    if url:
        content += f"  |  [View Post]({url})"
    if text:
        content += f"\n\n{text}"

    # Cap at Discord's 2000 char limit
    if len(content) > 2000:
        content = content[:1997] + "..."

    try:
        if images:
            files = {}
            handles = []
            try:
                for i, (path, name) in enumerate(images[:MAX_IMAGES]):
                    fh = open(path, "rb")
                    handles.append(fh)
                    files[f"files[{i}]"] = (name, fh)

                payload = {"content": content, "flags": 4}
                r = requests.post(
                    webhook,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=30,
                )
            finally:
                for fh in handles:
                    fh.close()
        else:
            r = requests.post(
                webhook,
                json={"content": content, "flags": 4},
                timeout=30,
            )

        if r.status_code in (200, 204):
            return True

        print(f"  [discord] Failed ({r.status_code}): {r.text[:200]}")
        return False

    except Exception as e:
        print(f"  [discord] Exception posting {post['id']}: {e}")
        return False
