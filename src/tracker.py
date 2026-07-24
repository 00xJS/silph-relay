import base64
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

SEEN_IDS_FILE = Path(os.getenv("SEEN_IDS_FILE", "data/seen_ids.json"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_SEEN_IDS_PATH = os.getenv("GITHUB_SEEN_IDS_PATH", "data/seen_ids.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

# Cached GitHub blob SHA for conditional updates
_github_sha = None


def _github_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_contents_url():
    return (
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_SEEN_IDS_PATH}"
    )


def load_seen_ids_from_github():
    """Load seen IDs from GitHub Contents API. Returns set or None on failure/skip."""
    global _github_sha
    if not _github_enabled():
        return None

    try:
        r = requests.get(
            _github_contents_url(),
            headers=_github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=20,
        )
        if r.status_code == 404:
            print("[tracker] No seen_ids on GitHub yet")
            _github_sha = None
            return set()
        r.raise_for_status()
        data = r.json()
        _github_sha = data.get("sha")
        raw = base64.b64decode(data["content"]).decode("utf-8")
        ids = set(json.loads(raw))
        print(f"[tracker] Loaded {len(ids)} seen IDs from GitHub")
        return ids
    except Exception as e:
        print(f"[tracker] GitHub load failed: {e}")
        return None


def save_seen_ids_to_github(ids):
    """Persist seen IDs to GitHub. No-op if not configured. Returns True on success."""
    global _github_sha
    if not _github_enabled():
        return False

    body = {
        "message": "chore: update seen post IDs [skip ci]",
        "content": base64.b64encode(
            json.dumps(sorted(list(ids)), indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if _github_sha:
        body["sha"] = _github_sha

    try:
        r = requests.put(
            _github_contents_url(),
            headers=_github_headers(),
            json=body,
            timeout=20,
        )
        if r.status_code in (200, 201):
            _github_sha = r.json().get("content", {}).get("sha") or _github_sha
            print(f"[tracker] Saved {len(ids)} seen IDs to GitHub")
            return True
        print(f"[tracker] GitHub save failed ({r.status_code}): {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[tracker] GitHub save exception: {e}")
        return False


def load_seen_ids():
    """
    Load previously processed post IDs.
    Prefers GitHub backup when configured, then falls back to local file.
    """
    remote = load_seen_ids_from_github()
    if remote is not None:
        # Keep local copy in sync for fast restarts within the same dyno
        _write_local(remote)
        return remote

    if SEEN_IDS_FILE.exists():
        with open(SEEN_IDS_FILE) as f:
            return set(json.load(f))
    return set()


def _write_local(ids):
    SEEN_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(sorted(list(ids)), f, indent=2)


def save_seen_ids(ids, previous=None):
    """
    Persist seen post IDs to disk.
    If GitHub backup is configured, also push when the set changed.
    """
    _write_local(ids)

    if previous is not None and set(ids) == set(previous):
        return

    save_seen_ids_to_github(ids)
