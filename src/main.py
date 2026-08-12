import os
import sys
import time

# Allow running from repo root
sys.path.insert(0, os.path.dirname(__file__))

from fetcher       import fetch_all, status_num, ACCOUNTS, FAST_ACCOUNTS
from tracker       import (load_seen_ids, save_seen_ids, load_deliveries, save_deliveries,
                           load_recent_posts, save_recent_posts, make_post_record, save_delta)
from discord_poster import post_to_discord

# Extra polls inside a single run, so latency isn't bounded by how often the
# workflow is triggered. 1 = poll once and exit (the long-standing behaviour).
# Only FAST_ACCOUNTS are re-checked; everything else is fetched on cycle 1.
POLL_CYCLES = max(1, int(os.getenv("POLL_CYCLES") or 1))

# Hard wall-clock budget for the whole run. This is the guardrail that matters:
# `timeout-minutes` starts counting when a job *runs*, not while it waits for a
# runner, so it cannot stop a run from bleeding into the next trigger.
POLL_WINDOW_SECONDS = float(os.getenv("POLL_WINDOW_SECONDS") or 45)


def publish(posts):
    """Post everything in `posts`. Returns (new_seen, delivered, log_rows, failed)."""
    posted, retry, delivered, log_rows, failed = set(), set(), [], [], 0

    for post in posts:
        print(f"  [main] Posting {post['account']['display']} — {post['id']}")
        success = post_to_discord(post)

        if success:
            now = int(time.time())
            sid = status_num(post["id"])
            posted.add(post["id"])
            delivered.append((sid, now))
            log_rows.append(make_post_record(post, sid, now))
            print(f"  [main] ✓ Posted")
        elif success is None:
            retry.add(post["id"])
            print(f"  [main] … Deferred — will retry next run")
        else:
            failed += 1
            print(f"  [main] ✗ Failed")

        for path, _ in post.get("images", []):
            try:
                os.remove(path)
            except Exception:
                pass

    new_seen = ({p["id"] for p in posts} - retry)
    return new_seen, delivered, log_rows, failed


def main():
    print("[main] Starting PokeUpdates bot run...")
    started = time.monotonic()

    seen_ids = load_seen_ids()
    print(f"[main] {len(seen_ids)} post IDs already seen")
    if POLL_CYCLES > 1:
        print(f"[main] Fast polling: {POLL_CYCLES} cycles over {POLL_WINDOW_SECONDS:.0f}s "
              f"for {', '.join(a['display'] for a in FAST_ACCOUNTS) or 'nobody'}")

    added_seen, all_delivered, all_log = set(), [], []
    total_posted = total_failed = 0

    for cycle in range(POLL_CYCLES):
        # Cycle 1 covers every account; later cycles only re-check the fast ones
        accounts = ACCOUNTS if cycle == 0 else FAST_ACCOUNTS
        if not accounts:
            break

        if cycle:
            # Spread remaining cycles evenly across what's left of the window,
            # and never start one we can't comfortably finish.
            elapsed = time.monotonic() - started
            slot = POLL_WINDOW_SECONDS * cycle / POLL_CYCLES
            if slot - elapsed > 0:
                time.sleep(slot - elapsed)
            if time.monotonic() - started > POLL_WINDOW_SECONDS - 5:
                print(f"[main] Out of time budget — stopping after {cycle} cycle(s)")
                break
            print(f"[main] --- poll {cycle + 1}/{POLL_CYCLES} ---")

        posts, skip_ids = fetch_all(seen_ids, accounts)
        print(f"[main] {len(posts)} new posts to publish")

        new_seen, delivered, log_rows, failed = publish(posts)
        new_seen |= skip_ids

        total_posted += len(delivered)
        total_failed += failed
        all_delivered.extend(delivered)
        all_log.extend(log_rows)
        added_seen |= new_seen

        # Fold into the working set so the next cycle doesn't re-post these,
        # and persist after every cycle so a crash can't lose what we sent.
        if new_seen:
            seen_ids |= new_seen
            save_seen_ids(seen_ids)
        if delivered:
            try:
                save_deliveries(load_deliveries() + delivered)
                save_recent_posts(load_recent_posts() + log_rows)
            except Exception as e:
                print(f"[main] Could not record delivery metrics: {e}")
        if added_seen or all_delivered:
            save_delta(new_seen=added_seen, deliveries=all_delivered, log_rows=all_log)

    if not added_seen and not all_delivered:
        print("[main] Nothing new — done.")
        return

    print(f"\n[main] Done — {total_posted} posted, {total_failed} failed "
          f"in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
