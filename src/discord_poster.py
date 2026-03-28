import os
import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Max images Discord accepts per message
MAX_IMAGES = 10


def post_to_discord(post):
    """
    Post a tweet to Discord via webhook.
    Sends text as message content and attaches images as files.
    Returns True on success, False on failure.
    """
    account = post["account"]
    text    = post["text"] or ""
    url     = post["url"]
    images  = post["images"]  # list of (tmp_path, filename)

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
            # Send with file attachments (up to MAX_IMAGES)
            files = {}
            for i, (path, name) in enumerate(images[:MAX_IMAGES]):
                files[f"files[{i}]"] = (name, open(path, "rb"))

            payload = {"content": content, "flags": 4}
            r = requests.post(WEBHOOK_URL, data={"payload_json": __import__("json").dumps(payload)}, files=files, timeout=30)

            # Close file handles
            for _, (_, fh) in files.items():
                fh.close()
        else:
            # Text-only post
            r = requests.post(WEBHOOK_URL, json={"content": content, "flags": 4}, timeout=30)

        if r.status_code in (200, 204):
            return True
        else:
            print(f"  [discord] Failed ({r.status_code}): {r.text[:200]}")
            return False

    except Exception as e:
        print(f"  [discord] Exception posting {post['id']}: {e}")
        return False
