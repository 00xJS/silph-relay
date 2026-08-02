import os
import sys

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

    posted_ids = set()
    retry_ids  = set()
    failed     = 0

    for post in posts:
        account_name = post["account"]["display"]
        print(f"  [main] Posting {account_name} — {post['id']}")

        success = post_to_discord(post)

        if success:
            posted_ids.add(post["id"])
            print(f"  [main] ✓ Posted")
        elif success is None:
            # Not delivered (no webhook configured, rate limited, network
            # error) — leave unseen so a later run retries it
            retry_ids.add(post["id"])
            print(f"  [main] … Deferred — will retry next run")
        else:
            failed += 1
            print(f"  [main] ✗ Failed")

        # Clean up temp image files
        for path, _ in post.get("images", []):
            try:
                os.remove(path)
            except Exception:
                pass

    # Mark posted + permanently-failed IDs as seen so we don't retry those
    # forever; deferred posts stay unseen and are picked up next run.
    # Pacing between sends is handled inside discord_poster.
    save_seen_ids((seen_ids | {p["id"] for p in posts} | skip_ids) - retry_ids)

    print(f"\n[main] Done — {len(posted_ids)} posted, {failed} failed, {len(retry_ids)} deferred")


if __name__ == "__main__":
    main()
