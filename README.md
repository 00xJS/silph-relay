# silph-relay

A lightweight relay that monitors Pokémon community accounts on X and posts new tweets — text and images — into Discord. Built for low latency on a **single free Render web service** (RSSHub + Python loop), with optional GitHub Actions one-shots.

---

## What It Does

| Account | Discord channel |
|---------|-----------------|
| **@PokemonGoApp** | updates (main webhook) |
| **@LeekDuck** | updates |
| **@thepokemodgroup** | updates |
| **@PokemonDealsTCG** | **drops-alerts** |

Every ~45s it polls one **combined search feed** (`from:A OR from:B OR …` via RSSHub's keyword route) that covers all four accounts in a single upstream call — that's what makes sub-minute polling safe on one X token. Every ~5 minutes it also sweeps each account's own feed as a backstop, catching anything X's search index silently filters (which matters for the deals account's affiliate links). Already-seen posts are skipped; new ones are forwarded via Discord webhooks with attached images.

---

## How It Works (Option A — recommended)

```
UptimeRobot (every 5 min) ──→  [ one Render free web service ]
                                  ├─ RSSHub  localhost:1200  (CACHE_EXPIRE=30, TWITTER_AUTH_TOKEN)
                                  ├─ Python loop  every 45s: combined-search feed → Discord
                                  │               every ~5 min: per-account feeds (backstop)
                                  └─ GET /health  (keep-alive + status)

seen_ids.json  →  local disk
               ↳  optional GitHub API backup when the set changes
```

Typical latency when scrapes are healthy: **~30s–2 minutes** after a tweet (not the old 30‑minute GitHub Actions cadence).

---

## Stack

- Python 3.11
- RSSHub (co-located in the same container)
- Discord webhooks (one per channel)
- Render free web service + UptimeRobot keep-alive
- Optional: GitHub Actions (`workflow_dispatch` only)

---

## Deploy on Render (primary)

### 1. Create Discord webhooks

- **Updates channel** → Integrations → Webhooks → copy URL  
- **`drops-alerts` channel** → separate webhook → copy URL  

### 2. New Web Service

- Connect this GitHub repo  
- **Runtime:** Docker (uses the included `Dockerfile`)  
- **Plan:** Free  
- **Health check path:** `/health`  

### 3. Environment variables

| Key | Value |
|-----|--------|
| `TWITTER_AUTH_TOKEN` | `auth_token` cookie of **one burner X account** (required — RSSHub's twitter routes fail without it; use a throwaway, never your personal account) |
| `DISCORD_WEBHOOK_UPDATES` | webhook for the main updates channel |
| `DISCORD_WEBHOOK_DROPS` | webhook for `drops-alerts` |
| `CACHE_EXPIRE` | `30` |
| `POLL_INTERVAL_SECONDS` | `45` (keep ≥ 40 on one token) |
| `SWEEP_INTERVAL_SECONDS` | `300` |
| `RSSHUB_URL` | `http://127.0.0.1:1200` |
| `RSSHUB_PORT` | `1200` |
| `GITHUB_TOKEN` | *(optional)* fine-grained PAT, Contents R/W |
| `GITHUB_REPO` | *(optional)* e.g. `00xJS/silph-relay` |
| `GITHUB_BRANCH` | `main` |
| `GITHUB_SEEN_IDS_PATH` | `data/seen_ids.json` |

Do **not** set `RUN_ONCE` on Render (leave unset so the process loops).

### 4. UptimeRobot

- Monitor type: **HTTP(s)**  
- URL: `https://<your-service>.onrender.com/health`  
- Interval: **5 minutes**  

This keeps the free dyno from sleeping.

### 5. Turn off the old 30‑minute Actions schedule

The workflow is already **manual-only** (`workflow_dispatch`). If you still have a separate always-on RSSHub service, you can leave it or delete it once the combined service is stable.

---

## Local / one-shot run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill webhooks + RSSHUB_URL
RUN_ONCE=1 python src/main.py
```

For a local long-running loop (health on `:10000`):

```bash
# point RSSHUB_URL at any reachable RSSHub instance
unset RUN_ONCE   # or RUN_ONCE=0
python src/main.py
```

---

## GitHub Actions (manual backup only)

Secrets (Settings → Secrets → Actions):

- `RSSHUB_URL` — public RSSHub if you keep one; not the internal `127.0.0.1` URL  
- `DISCORD_WEBHOOK_UPDATES`  
- `DISCORD_WEBHOOK_DROPS`  

Run from the Actions tab → **PokeUpdates Bot** → **Run workflow**.  
Avoid running this while the Render loop is healthy unless you share `seen_ids` (GitHub backup) or you may double-post.

---

## Project structure

```
silph-relay/
├── src/
│   ├── config.py         # Accounts, webhooks, env
│   ├── fetcher.py        # RSSHub → posts + images
│   ├── tracker.py        # seen_ids local + optional GitHub
│   ├── discord_poster.py # Webhook posts
│   └── main.py           # Loop + /health or RUN_ONCE
├── data/seen_ids.json
├── Dockerfile            # RSSHub + relay
├── start.sh
├── render.yaml
├── .env.example
└── .github/workflows/pipeline.yml
```

---

## Deduplication

`data/seen_ids.json` stores every post ID that has been attempted. On Render free tier the disk is **ephemeral**, so set `GITHUB_TOKEN` + `GITHUB_REPO` to push `seen_ids` only when it changes. On restart the loop reloads from GitHub first.

---

## Latency notes

| Layer | Role |
|-------|------|
| Combined search feed | One upstream call covers all 4 accounts → sub-minute polling fits one token's ~50 req/15 min search budget |
| Per-account sweep | Every ~5 min; separate upstream budget; catches search-filtered tweets |
| Degraded mode | If the search route breaks (503s), sweeps auto-speed-up to every ~2 min and recover automatically |
| `CACHE_EXPIRE=30` | RSSHub cache TTL |
| `POLL_INTERVAL_SECONDS=45` | How often we re-fetch (keep ≥ 40s on one token) |
| X scrape reliability | Main source of occasional multi‑minute delays |

Sub‑minute delivery is common when RSSHub’s Twitter route is healthy; it is **not** guaranteed. True &lt;5s streaming needs a paid X data API.

---

*Silph Relay is a fan-made tool and is not affiliated with Niantic, The Pokémon Company, or any of the accounts it monitors.*
