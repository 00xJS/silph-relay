import os
import sys
import time

# Allow running from repo root
sys.path.insert(0, os.path.dirname(__file__))

from fetcher       import fetch_all
from tracker       import load_seen_ids, save_seen_ids
from discord_poster import post_to_discord


def main():
    print("[main] Starting PokeUpdates bot run...")

    seen_ids = load_seen_ids()
    print(f"[main] {len(seen_ids)} post IDs already seen")

    posts, skip_ids = fetch_all(seen_ids)
    print(f"[main] {len(posts)} new posts to publish")

    if not posts:
        if skip_ids:
            save_seen_ids(seen_ids | skip_ids)
        print("[main] Nothing new — done.")
        return

    posted_ids       = set()
    unconfigured_ids = set()
    failed           = 0

    for post in posts:
        account_name = post["account"]["display"]
        print(f"  [main] Posting {account_name} — {post['id']}")

        success = post_to_discord(post)

        if success:
            posted_ids.add(post["id"])
            print(f"  [main] ✓ Posted")
        elif success is None:
            # No webhook configured for this account — leave unseen so it
            # posts once the secret is added
            unconfigured_ids.add(post["id"])
        else:
            failed += 1
            print(f"  [main] ✗ Failed")

        # Clean up temp image files
        for path, _ in post.get("images", []):
            try:
                os.remove(path)
            except Exception:
                pass

        # Small delay to avoid hitting Discord rate limits
        time.sleep(1)

    # Save all new IDs (posted + failed) so we don't retry failed posts
    # indefinitely — but keep unconfigured-webhook posts retryable
    save_seen_ids((seen_ids | {p["id"] for p in posts} | skip_ids) - unconfigured_ids)

    print(f"\n[main] Done — {len(posted_ids)} posted, {failed} failed, {len(unconfigured_ids)} awaiting webhook config")


if __name__ == "__main__":
    main()
