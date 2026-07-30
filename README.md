# silph-relay

A lightweight Discord bot that monitors Pokémon GO community accounts on X and automatically relays their posts — text and images — to a Discord channel. No manual reposting, no missed updates.

---

## What It Does

Silph Relay watches five accounts, split across two Discord channels:

**Pokémon GO channel** (`DISCORD_WEBHOOK_URL`):
- **@PokemonGoApp** — official Pokémon GO announcements
- **@LeekDuck** — event calendars, raid infographics, and datamines
- **@thepokemodgroup** — asset updates and community datamines

**TCG restocks & deals channel** (`DISCORD_WEBHOOK_URL_RESTOCKS`):
- **@pokemonrestocks** — Pokémon TCG restock alerts
- **@PokemonDealsTCG** — Pokémon TCG deals

Every few minutes, it checks for new posts and forwards them to the right Discord channel with the full post text and all attached images. Already-seen posts are tracked so nothing gets double-posted, and each account is capped at 5 posts per run so a newly added account's backlog never floods a channel.

---

## How It Works

```
FxTwitter API (primary) ──┐
RSSHub feed  (fallback) ──┴→ fetcher.py → discord_poster.py → Discord webhooks
                                       ↕
                                tracker (seen_ids.json)
```

1. **fetcher.py** pulls each account's recent posts (all accounts in parallel) from [FxTwitter's](https://docs.fxembed.com) free no-auth JSON API, automatically falling back to an RSSHub feed if that fails
2. Already-seen posts (matched by numeric status ID), replies to other accounts, and stale posts (>24h) are filtered out; images are downloaded
3. **discord_poster.py** sends each new post to its account's Discord webhook with images attached
4. **seen_ids.json** is committed back to the repo after each run to persist deduplication across GitHub Actions runs

---

## Stack

- Python 3.11
- GitHub Actions, triggered every few minutes by [cron-job.org](https://cron-job.org) via `workflow_dispatch` (GitHub's own cron is best-effort and kept only as a 15-minute backstop)
- FxTwitter API (free, no auth — primary post source)
- RSSHub (automatic fallback; a public instance works out of the box)
- Discord Webhooks (one per channel)

---

## Self-Hosting

### Requirements
- A [cron-job.org](https://cron-job.org) account (free) to trigger the pipeline reliably
- A Discord server with a webhook URL
- A GitHub account to host and run the bot
- Optional: an RSSHub instance URL for the fallback path

### Setup

**1. Post sources — nothing to configure**

Posts come from [FxTwitter's](https://docs.fxembed.com) free JSON API by default: no key, no account, no setup. Optionally set `RSSHUB_URL` (a public [RSSHub](https://github.com/DIYgod/RSSHub) instance works) as an automatic fallback source — and if you self-host RSSHub with an `ACCESS_KEY`, mirror it in the `RSSHUB_ACCESS_KEY` secret.

**2. Create a Discord Webhook**

In your Discord server: Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.

**3. Clone and configure**

```bash
git clone https://github.com/00xJS/silph-relay.git
cd silph-relay
cp .env.example .env
```

Fill in your `.env`:
```
RSSHUB_URL=https://your-rsshub-instance.onrender.com
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
DISCORD_WEBHOOK_URL_RESTOCKS=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
```

**4. Add GitHub Secrets**

In your repo: Settings → Secrets and variables → Actions → add:
- `RSSHUB_URL`
- `RSSHUB_ACCESS_KEY` (optional — only if your self-hosted instance sets `ACCESS_KEY`)
- `DISCORD_WEBHOOK_URL`
- `DISCORD_WEBHOOK_URL_RESTOCKS`

**5. Set up the trigger**

GitHub's own cron is best-effort (often hours late), so the pipeline is fired externally: create a free [cron-job.org](https://cron-job.org) job that POSTs to
`https://api.github.com/repos/<you>/<repo>/actions/workflows/pipeline.yml/dispatches`
with body `{"ref":"main"}` and an `Authorization: Bearer <fine-grained PAT>` header (PAT scope: this repo only, Actions read/write), every 3–5 minutes. Test first from the Actions tab → PokeUpdates Bot → Run workflow.

---

## Project Structure

```
silph-relay/
├── src/
│   ├── fetcher.py        # Pulls posts from RSSHub, downloads images
│   ├── tracker.py        # Loads and saves seen post IDs
│   ├── discord_poster.py # Sends posts to Discord via webhook
│   └── main.py           # Orchestrates the full run
├── data/
│   └── seen_ids.json     # Tracks which posts have already been relayed
├── .github/workflows/
│   └── pipeline.yml      # GitHub Actions run config (dispatch-triggered)
├── .env.example
└── requirements.txt
```

---

## Deduplication

`seen_ids.json` stores the ID of every post that has been relayed. Since GitHub Actions has no persistent filesystem between runs, the workflow commits this file back to the repo after each run (tagged `[skip ci]` to prevent loops). On the next run, the updated file is checked out and already-seen posts are skipped.

---

*Silph Relay is a fan-made tool and is not affiliated with Niantic, The Pokémon Company, or any of the accounts it monitors.*
