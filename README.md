# silph-relay

A lightweight Discord bot that monitors Pokémon GO community accounts on X and automatically relays their posts — text and images — to a Discord channel. No manual reposting, no missed updates.

---

## What It Does

Silph Relay watches three accounts:

- **@PokemonGoApp** — official Pokémon GO announcements
- **@LeekDuck** — event calendars, raid infographics, and datamines
- **@thepokemodgroup** — asset updates and community datamines

Every 30 minutes, it checks for new posts and forwards them to your Discord channel with the full post text and all attached images. Already-seen posts are tracked so nothing gets double-posted.

---

## How It Works

```
RSSHub (self-hosted) → fetcher.py → discord_poster.py → Discord webhook
                                  ↕
                           tracker (seen_ids.json)
```

1. **RSSHub** scrapes the three X accounts and serves them as RSS feeds
2. **fetcher.py** pulls the feeds, filters out already-seen posts, and downloads any images
3. **discord_poster.py** sends each new post to Discord via webhook with images attached
4. **seen_ids.json** is committed back to the repo after each run to persist deduplication across GitHub Actions runs

---

## Stack

- Python 3.11
- GitHub Actions (runs on a 30-minute cron schedule)
- RSSHub (self-hosted on Render — free tier)
- Discord Webhook

---

## Self-Hosting

### Requirements
- A [Render](https://render.com) account (free) to host RSSHub
- A Discord server with a webhook URL
- A GitHub account to host and run the bot

### Setup

**1. Deploy RSSHub on Render**

Create a new Web Service on Render pointing to `https://github.com/DIYgod/RSSHub`. Free tier works. Copy the URL it gives you.

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
```

**4. Add GitHub Secrets**

In your repo: Settings → Secrets and variables → Actions → add:
- `RSSHUB_URL`
- `DISCORD_WEBHOOK_URL`

**5. Enable GitHub Actions**

Go to the Actions tab → PokeUpdates Bot → Run workflow to trigger a manual test. If successful, it will run automatically every 30 minutes from that point on.

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
│   └── pipeline.yml      # GitHub Actions schedule and run config
├── .env.example
└── requirements.txt
```

---

## Deduplication

`seen_ids.json` stores the ID of every post that has been relayed. Since GitHub Actions has no persistent filesystem between runs, the workflow commits this file back to the repo after each run (tagged `[skip ci]` to prevent loops). On the next run, the updated file is checked out and already-seen posts are skipped.

---

*Silph Relay is a fan-made tool and is not affiliated with Niantic, The Pokémon Company, or any of the accounts it monitors.*
