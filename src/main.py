import json
import os
import sys
import time
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, os.path.dirname(__file__))

from fetcher        import fetch_all, status_num, ACCOUNTS, FAST_ACCOUNTS
from tracker        import (load_seen_ids, save_seen_ids, load_deliveries, save_deliveries,
                            load_recent_posts, save_recent_posts, make_post_record, save_delta)
from discord_poster import post_to_discord, REJECTED_WEBHOOKS

# Extra polls inside a single run, so latency isn't bounded by how often the
# workflow is triggered. 1 = poll once and exit (the long-standing behaviour).
# Only FAST_ACCOUNTS are re-checked; everything else is fetched on poll 1.
POLL_CYCLES = max(1, int(os.getenv("POLL_CYCLES") or 1))

# How the extra polls are timed:
#   clock  (default) — at fixed seconds past the minute, spread evenly across it.
#          Runs are dispatched once a minute, so fixed anchors mesh with the next
#          run's first poll instead of leaving one long blind gap after the last
#          poll: with 3 polls, about :08/:28/:48 rather than :10/:22/:33. Same
#          request count; the longest wait for a restock post drops from ~37s to
#          ~20s. A run that starts late fits in fewer polls rather than running
#          into the next minute.
#   window — the original behaviour: spread across POLL_WINDOW_SECONDS from the
#          start of the run. Kept as the rollback lever.
POLL_MODE = (os.getenv("POLL_MODE") or "clock").strip().lower()

# window mode only. Hard wall-clock budget for the whole run — `timeout-minutes`
# starts counting when a job *runs*, not while it waits for a runner, so it
# cannot stop a run from bleeding into the next trigger.
POLL_WINDOW_SECONDS = float(os.getenv("POLL_WINDOW_SECONDS") or 45)

# clock mode only. Never START a poll later than this many seconds past the
# minute, so the run is finished before the next dispatch (fired at :00).
POLL_DEADLINE_SECOND = float(os.getenv("POLL_DEADLINE_SECOND") or 50)
MIN_POLL_GAP = 5.0  # polls closer together than this add requests, not coverage

# This run's verdict, for the Heartbeat step. Untracked, like the delta.
HEALTH_FILE = Path("data/.health.json")

FAST_HANDLE_SET = {a["handle"] for a in FAST_ACCOUNTS}


def clock_schedule(first_poll, cycles, deadline=POLL_DEADLINE_SECOND, min_gap=MIN_POLL_GAP):
    """Unix times for polls 2..N, anchored to the wall clock.

    Spreads the N polls evenly across the minute (every 60/N seconds) from the
    first one. If that would run past `deadline` seconds into the minute — the
    run started late — the remaining polls are packed evenly into the time
    left, dropping any that would land closer than `min_gap` apart.
    """
    if cycles < 2:
        return []
    second = first_poll % 60
    room = deadline - second
    if room < min_gap:
        return []
    extra = cycles - 1
    spacing = 60 / cycles
    if second + extra * spacing > deadline:
        extra = min(extra, int(room // min_gap))
        spacing = room / extra
    return [first_poll + i * spacing for i in range(1, extra + 1)]


def wait_for_poll(n, started, first_poll, last_poll, schedule):
    """Sleep until poll number n (2, 3, ...) is due.

    Returns "poll", "skip" (a poll only just ran, so this slot is covered) or
    "stop" (out of time for this run).
    """
    if POLL_MODE == "window":
        elapsed = time.monotonic() - started
        slot = POLL_WINDOW_SECONDS * (n - 1) / POLL_CYCLES
        if slot - elapsed > 0:
            time.sleep(slot - elapsed)
        if time.monotonic() - started > POLL_WINDOW_SECONDS - 5:
            return "stop"
        return "poll"

    if n - 2 >= len(schedule):
        return "stop"
    now = time.time()
    if now - (first_poll - first_poll % 60) > POLL_DEADLINE_SECOND:
        return "stop"
    due = schedule[n - 2]
    if due > now:
        time.sleep(due - now)
    elif now - last_poll < MIN_POLL_GAP:
        return "skip"
    return "poll"


def posting_order(post):
    """Time-sensitive accounts first; within that, oldest first, so a burst
    reads top-to-bottom in the channel in the order it was posted on X."""
    try:
        num = int(status_num(post["id"]))
    except ValueError:
        num = 0
    return (post["account"]["handle"] not in FAST_HANDLE_SET, num)


def publish(posts):
    """Post everything in `posts`. Returns (new_seen, delivered, log_rows, failed, deferred)."""
    retry, delivered, log_rows, failed = set(), [], [], 0

    for post in sorted(posts, key=posting_order):
        print(f"  [main] Posting {post['account']['display']} — {post['id']}")
        success = post_to_discord(post)

        if success:
            now = int(time.time())
            sid = status_num(post["id"])
            delivered.append((sid, now))
            log_rows.append(make_post_record(post, sid, now))
            print(f"  [main] ✓ Posted")
        elif success is None:
            retry.add(post["id"])
            print(f"  [main] … Deferred — will retry next run")
        else:
            failed += 1
            print(f"  [main] ✗ Failed")

    new_seen = {p["id"] for p in posts} - retry
    return new_seen, delivered, log_rows, failed, len(retry)


def write_health(problems, summary):
    """Leave this run's verdict for the Heartbeat step. Never raises."""
    ok = not problems
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.write_text(json.dumps({"ok": ok, "summary": summary, "problems": problems}))
    except Exception as e:
        print(f"[main] Could not write the health report: {e}")
    print(f"[main] Health: {'OK' if ok else 'PROBLEM'} — {summary}")
    for problem in problems:
        print(f"[main] !! {problem}")


def main():
    started = time.monotonic()
    print(f"[main] Starting silph-relay run (Python {sys.version.split()[0]})")

    seen_ids = load_seen_ids()
    print(f"[main] {len(seen_ids)} post IDs already seen")

    added_seen, all_delivered, all_log = set(), [], []
    total_posted = total_failed = total_deferred = polls = 0
    answered = {}        # source -> account fetches it served this run
    unanswered = set()   # handles no source answered for, on some poll
    first_poll = last_poll = None
    schedule = []

    for n in range(1, POLL_CYCLES + 1):
        # Poll 1 covers every account; later polls only re-check the fast ones
        accounts = ACCOUNTS if n == 1 else FAST_ACCOUNTS
        if not accounts:
            break

        if n > 1:
            step = wait_for_poll(n, started, first_poll, last_poll, schedule)
            if step == "stop":
                print(f"[main] Out of time — stopping after {polls} poll(s)")
                break
            if step == "skip":
                continue
            print(f"[main] --- poll {n}/{POLL_CYCLES} ---")

        last_poll = time.time()
        if n == 1 and POLL_CYCLES > 1:
            first_poll = last_poll
            fast = ", ".join(a["display"] for a in FAST_ACCOUNTS) or "nobody"
            if POLL_MODE == "window":
                print(f"[main] Fast polling: {POLL_CYCLES} cycles over {POLL_WINDOW_SECONDS:.0f}s for {fast}")
            else:
                schedule = clock_schedule(first_poll, POLL_CYCLES)
                marks = ", ".join(f":{int(t % 60):02d}" for t in [first_poll] + schedule)
                note = "" if len(schedule) == POLL_CYCLES - 1 else " (late start — fewer polls fit)"
                print(f"[main] Fast polling: polls at {marks} for {fast}{note}")
        polls += 1

        posts, skip_ids, sources = fetch_all(seen_ids, accounts)
        for handle, source in sources.items():
            if source:
                answered[source] = answered.get(source, 0) + 1
            else:
                unanswered.add(handle)
        print(f"[main] {len(posts)} new posts to publish")

        new_seen, delivered, log_rows, failed, deferred = publish(posts)
        new_seen |= skip_ids

        total_posted += len(delivered)
        total_failed += failed
        total_deferred += deferred
        all_delivered.extend(delivered)
        all_log.extend(log_rows)
        added_seen |= new_seen

        # Fold into the working set so the next poll doesn't re-post these,
        # and persist after every poll so a crash can't lose what we sent.
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

    # The verdict the heartbeat monitor sees. Only conditions a green run would
    # otherwise hide count as problems; one-off blips (a deferred post, a single
    # account timing out) are just noted in the summary.
    problems = []
    if not answered:
        problems.append("no source answered for any account — FxTwitter and every Nitter instance failed")
    for env in sorted(REJECTED_WEBHOOKS):
        problems.append(f"Discord rejected the {env} webhook — that channel's posts are being held")
    summary = (f"posted {total_posted}, deferred {total_deferred}, failed {total_failed}; "
               f"{polls} poll(s); sources: "
               + (", ".join(f"{src} x{count}" for src, count in sorted(answered.items())) or "none"))
    if unanswered and answered:
        summary += f"; no answer for {', '.join(sorted(unanswered))}"
    write_health(problems, summary)

    if not added_seen and not all_delivered:
        print("[main] Nothing new — done.")
        return

    print(f"\n[main] Done — {total_posted} posted, {total_failed} failed "
          f"in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
