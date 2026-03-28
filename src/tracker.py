import json
from pathlib import Path

SEEN_IDS_FILE = Path("data/seen_ids.json")


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
