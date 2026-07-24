"""Shared configuration for silph-relay.

Reads everything from environment variables (a local .env is loaded for
development). The relay polls X via twscrape using burner-account cookies and
forwards new tweets to per-account Discord webhooks.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── Discord webhooks ─────────────────────────────────────────────────────────
# updates → main relay channel;  drops → #drops-alerts channel.
# DISCORD_WEBHOOK_URL is honored as a back-compat alias for the updates webhook.
DISCORD_WEBHOOK_UPDATES = os.getenv(
    "DISCORD_WEBHOOK_UPDATES", os.getenv("DISCORD_WEBHOOK_URL", "")
)
DISCORD_WEBHOOK_DROPS = os.getenv("DISCORD_WEBHOOK_DROPS", "")

# ── RSSHub ───────────────────────────────────────────────────────────────────
# The relay reads X through a co-located RSSHub instance (see Dockerfile).
# RSSHub itself needs TWITTER_AUTH_TOKEN — the auth_token cookie of ONE burner
# X account — set on the service; without it the twitter routes return errors.
RSSHUB_URL = os.getenv("RSSHUB_URL", "http://127.0.0.1:1200").rstrip("/")

# ── Poll cadence ─────────────────────────────────────────────────────────────
# Fast path: RSSHub's keyword route with one combined `from:A OR from:B ...`
# query covers every account in a single upstream search call, so it can run
# sub-minute on one token. Backstop: the per-account user routes run every Nth
# cycle to catch anything X's search index silently filters (matters most for
# the deals account posting affiliate links). The two use separate upstream
# rate-limit budgets and never contend.
#
# One-token budget guide: keep POLL_INTERVAL_SECONDS >= 40. Faster than that
# risks tripping X's ~50 req/15min search budget and locking the token.
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 45)
SWEEP_INTERVAL = _int("SWEEP_INTERVAL_SECONDS", 300)
# When the search feed is unavailable (route broken/locked), the per-account
# sweep speeds up to this cadence instead — the fastest per-account polling
# that stays inside one token's UserTweets budget (4 calls per sweep).
SWEEP_INTERVAL_DEGRADED = _int("SWEEP_INTERVAL_DEGRADED_SECONDS", 120)
JITTER = _int("POLL_JITTER_SECONDS", 15)
DISCORD_POST_DELAY = _float("DISCORD_POST_DELAY", 1.0)
MAX_POST_RETRIES = _int("MAX_POST_RETRIES", 5)

# ── Runtime ──────────────────────────────────────────────────────────────────
RUN_ONCE = _bool("RUN_ONCE", False)  # single pass then exit (local/manual test)
PORT = _int("PORT", 10000)  # Render injects this; health server binds it
# On a fresh install (empty seen_ids) mark existing tweets as seen WITHOUT
# posting, so the first deploy doesn't flood the channel with old tweets.
PRIME_ON_EMPTY = _bool("PRIME_ON_EMPTY", True)

# ── seen_ids persistence ─────────────────────────────────────────────────────
# Render's free filesystem is ephemeral and restarts at any time, so seen IDs
# are mirrored to a file in the GitHub repo via the Contents API. Without a
# token the relay still works but may repost a few tweets after a restart.
LOCAL_SEEN_PATH = os.getenv("LOCAL_SEEN_PATH", "data/seen_ids.json")
SEEN_IDS_MAX = _int("SEEN_IDS_MAX", 1000)
FLUSH_MIN_INTERVAL = _int("FLUSH_MIN_INTERVAL_SECONDS", 60)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # e.g. 00xJS/silph-relay
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_SEEN_IDS_PATH = os.getenv("GITHUB_SEEN_IDS_PATH", "data/seen_ids.json")

# ── Accounts → channel ───────────────────────────────────────────────────────
# webhook: "updates" | "drops"
ACCOUNTS = [
    {"handle": "PokemonGoApp", "display": "@PokemonGoApp", "webhook": "updates"},
    {"handle": "LeekDuck", "display": "@LeekDuck", "webhook": "updates"},
    {"handle": "thepokemodgroup", "display": "@thepokemodgroup", "webhook": "updates"},
    {"handle": "PokemonDealsTCG", "display": "@PokemonDealsTCG", "webhook": "drops"},
]


def webhook_for(account):
    """Resolve the Discord webhook URL for an account."""
    if account.get("webhook") == "drops":
        return DISCORD_WEBHOOK_DROPS
    return DISCORD_WEBHOOK_UPDATES
