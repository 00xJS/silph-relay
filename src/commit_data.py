"""Publish this run's data changes, safely, even when another run raced us.

Why this exists: the data files are append-only logs that two runs can touch
at once. Rebasing a local commit onto a moved origin/main produced hard
conflicts and failed the whole job — which left seen_ids unpublished, so the
next run re-posted the same tweets and failed the same way.

This never rebases. Each attempt starts from a freshly fetched origin/main and
re-applies the run's delta on top, so a concurrent push costs a retry, not a
conflict. Nothing here can lose records: the delta is re-applied in full and
the save_* helpers de-duplicate.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from tracker import (DELTA_FILE, load_seen_ids, save_seen_ids,
                     load_deliveries, save_deliveries,
                     load_recent_posts, save_recent_posts)

MAX_ATTEMPTS = 6
DATA_FILES = ["data/seen_ids.json", "data/deliveries.json", "data/recent_posts.json"]


def git(*args, check=True):
    r = subprocess.run(("git",) + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r


def apply_delta(delta):
    """Union the run's additions into whatever is currently on disk."""
    if delta.get("seen"):
        save_seen_ids(load_seen_ids() | set(delta["seen"]))
    if delta.get("deliveries"):
        save_deliveries(load_deliveries() + [(str(a), int(b)) for a, b in delta["deliveries"]])
    if delta.get("log"):
        save_recent_posts(load_recent_posts() + delta["log"])


def main():
    if not DELTA_FILE.exists():
        print("[commit] No delta from this run — nothing to publish")
        return 0

    try:
        delta = json.loads(DELTA_FILE.read_text())
    except Exception as e:
        print(f"[commit] Delta unreadable ({e}) — nothing to publish")
        return 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Start from the current remote every time, then re-apply our delta —
        # this is what makes a concurrent push harmless.
        git("fetch", "--quiet", "origin", "main")
        git("reset", "--quiet", "--hard", "FETCH_HEAD")   # leaves the untracked delta alone

        apply_delta(delta)

        git("add", *DATA_FILES)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("[commit] Already published by another run — nothing to do")
            return 0

        git("commit", "--quiet", "-m", "chore: update seen post IDs [skip ci]")

        if git("push", "--quiet", "origin", "HEAD:main", check=False).returncode == 0:
            print(f"[commit] Published on attempt {attempt}")
            return 0

        print(f"[commit] Remote moved (attempt {attempt}/{MAX_ATTEMPTS}) — retrying on fresh main")
        time.sleep(1.5 * attempt)

    print(f"[commit] ERROR: could not publish after {MAX_ATTEMPTS} attempts")
    return 1


if __name__ == "__main__":
    sys.exit(main())
