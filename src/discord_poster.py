import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Max images Discord accepts per message
MAX_IMAGES = 10


def post_to_discord(post):
    """
    Post a tweet to Discord via the webhook configured for its account.
    Sends text as message content and attaches images as files.
    Returns True on success, False on failure, None if no webhook is configured
    (so the caller can leave the post unseen and retry once configured).
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

    try:
        if images:
            # Send with file attachments (up to MAX_IMAGES)
            files = {}
            for i, (path, name) in enumerate(images[:MAX_IMAGES]):
                files[f"files[{i}]"] = (name, open(path, "rb"))

            payload = {"content": content, "flags": 4}
            r = requests.post(webhook_url, data={"payload_json": __import__("json").dumps(payload)}, files=files, timeout=30)

            # Close file handles
            for _, (_, fh) in files.items():
                fh.close()
        else:
            # Text-only post
            r = requests.post(webhook_url, json={"content": content, "flags": 4}, timeout=30)

        if r.status_code in (200, 204):
            return True
        else:
            print(f"  [discord] Failed ({r.status_code}): {r.text[:200]}")
            return False

    except Exception as e:
        print(f"  [discord] Exception posting {post['id']}: {e}")
        return False
