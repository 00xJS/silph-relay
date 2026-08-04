import json
from pathlib import Path

SEEN_IDS_FILE = Path("data/seen_ids.json")

# When each post reached Discord: [[status_id, unix_seconds], ...].
# The tweet's own creation time is encoded in its ID, so this is all the
# dashboard needs to derive delivery latency.
DELIVERIES_FILE = Path("data/deliveries.json")
MAX_DELIVERIES  = 1000


def load_seen_ids():
    """Load previously processed post IDs from disk."""
    if SEEN_IDS_FILE.exists():
        with open(SEEN_IDS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    """Persist seen post IDs to disk."""
    SEEN_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(sorted(list(ids)), f, indent=2)


def load_deliveries():
    """Load [status_id, unix_seconds] delivery records. Never raises."""
    if not DELIVERIES_FILE.exists():
        return []
    try:
        with open(DELIVERIES_FILE) as f:
            data = json.load(f)
        return [(str(r[0]), int(r[1])) for r in data if isinstance(r, (list, tuple)) and len(r) == 2]
    except Exception as e:
        print(f"  [tracker] Couldn't read {DELIVERIES_FILE}: {e}")
        return []


def save_deliveries(records):
    """Persist delivery records, de-duplicated and capped to the newest MAX."""
    newest = {}
    for sid, ts in records:
        if sid not in newest or ts < newest[sid]:
            newest[sid] = int(ts)
    trimmed = sorted(newest.items(), key=lambda r: r[1])[-MAX_DELIVERIES:]
    DELIVERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DELIVERIES_FILE, "w") as f:
        json.dump([[s, t] for s, t in trimmed], f, separators=(",", ":"))
