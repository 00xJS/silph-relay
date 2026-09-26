# silph-relay

A lightweight Discord bot that monitors Pokémon accounts on X and relays their posts — text and images — to Discord within seconds. Pokémon GO news and Pokémon TCG restock alerts go to separate channels, so time-sensitive drops don't get buried. No manual reposting, no missed updates.

Live dashboard: **[silph-relay.netlify.app](https://silph-relay.netlify.app/)**

---

## What It Does

Silph Relay watches six accounts, split across two Discord channels:

**Pokémon GO channel** (`DISCORD_WEBHOOK_URL`):
- **@PokemonGoApp** — official Pokémon GO announcements
- **@LeekDuck** — event calendars, raid infographics, and datamines
- **@thepokemodgroup** — asset updates and community datamines
- **@ScopelyExplore** — Scopely service updates and known-issue notices

**TCG restocks & deals channel** (`DISCORD_WEBHOOK_URL_RESTOCKS`):
- **@pokemonrestocks** — Pokémon TCG restock alerts
- **@PokemonDealsTCG** — Pokémon TCG deals

Every minute it checks for new posts — the two restock accounts three times a minute — and forwards each one to the right channel with the full post text and all attached images, under the account's own name and avatar. A burst arrives in the order it was posted. A post that reaches Discord late (say, after an outage) carries a "posted 2 hours ago" stamp, so an old restock is never mistaken for a live one. Already-relayed posts are tracked so nothing is posted twice, and each account is capped at 5 posts per run so a newly added account's backlog never floods a channel.

---

## How It Works

```
FxTwitter API   (primary) ──┐
Nitter RSS feeds (fallback) ─┴→ fetcher.py → discord_poster.py → Discord webhooks
                                      ↕
                         tracker.py (data/*.json, committed back)
```

1. **fetcher.py** pulls each account's recent posts (all accounts in parallel) from [FxTwitter's](https://docs.fxembed.com) free no-auth JSON API. If FxTwitter fails for an account, it falls back to a list of Nitter instances' RSS feeds, in order. Both report X's own post IDs, so switching source can't cause repeats.
2. Already-relayed posts (matched by numeric post ID), replies to other accounts, and stale posts (>24h) are filtered out; images are downloaded.
3. **discord_poster.py** sends each new post to its channel's webhook, with the images attached.
4. The data files are committed back to the repo after each run, so the next run knows what has already been relayed.

It uses nothing but the Python 3 standard library, so a run installs nothing and never depends on PyPI being up.

### Staying up

Two things run alongside the relay so a failure is fixed or reported instead of going unnoticed:

- **Watchdog** (`watchdog.yml`, every 5 minutes): if the relay has had no successful run for 10 minutes, it cancels whatever run is stuck. On 2026-09-12 a single run that GitHub never started blocked every run behind it for 24 hours. Once an hour it also checks that at least one Nitter fallback still answers.
- **Heartbeat** (optional, [healthchecks.io](https://healthchecks.io) free tier): every healthy run pings a check. If the pings stop, you get an alert. That covers every way the relay can go quiet: stuck runs, an expired dispatch token, a scheduler outage, or every post source down at once.

---

## Stack

- Python 3 (standard library only — no dependencies)
- GitHub Actions, triggered every minute by [cron-job.org](https://cron-job.org) via `workflow_dispatch` (GitHub's own cron is best-effort and kept only as a backstop)
- FxTwitter API (free, no auth — primary post source)
- Nitter RSS (automatic fallback; public instances, no auth)
- Discord Webhooks (one per channel)
- healthchecks.io (optional heartbeat alerts)

---

## Self-Hosting

### Requirements
- A [cron-job.org](https://cron-job.org) account (free) to trigger the workflows reliably
- A Discord server with a webhook per channel
- A GitHub account to host and run the bot
- Optional: a [healthchecks.io](https://healthchecks.io) account (free) for alerts

### Setup

**1. Post sources — nothing to configure**

Posts come from [FxTwitter's](https://docs.fxembed.com) free JSON API: no key, no account, no setup. The Nitter fallback defaults to instances that were working in September 2026. Nitter instances come and go, so you can override them with the `NITTER_BASE` variable (below).

**2. Create the Discord webhooks**

In your Discord server: Edit Channel → Integrations → Webhooks → New Webhook → Copy URL. One for the Pokémon GO channel, one for the restocks channel.

**3. Clone**

```bash
git clone https://github.com/00xJS/silph-relay.git
cd silph-relay
cp .env.example .env   # only needed to run it on your own machine: python3 src/main.py
```

**4. Add GitHub Secrets**

In your repo: Settings → Secrets and variables → Actions → Secrets:
- `DISCORD_WEBHOOK_URL`
- `DISCORD_WEBHOOK_URL_RESTOCKS`
- `HEALTHCHECK_URL` (optional) — a healthchecks.io check's ping URL. Set the check's period to 1 minute and its grace time to 10 minutes.
- `HEALTHCHECK_FALLBACK_URL` (optional) — a second check for the hourly fallback test. Set period to 1 hour and grace to 1 day.

**5. Set up the triggers**

GitHub's own cron is best-effort (often hours late), so both workflows are fired externally. Create a [fine-grained PAT](https://github.com/settings/personal-access-tokens) scoped to this repo only, with Actions read/write. Then create two cron-job.org jobs, each a `POST` with body `{"ref":"main"}` and headers `Authorization: Bearer <PAT>` and `Accept: application/vnd.github+json`:

| Job | URL | Schedule |
|---|---|---|
| Relay | `https://api.github.com/repos/<you>/<repo>/actions/workflows/pipeline.yml/dispatches` | every minute |
| Watchdog | `https://api.github.com/repos/<you>/<repo>/actions/workflows/watchdog.yml/dispatches` | every 5 minutes |

Test first from the Actions tab → PokeUpdates Bot → Run workflow.

**6. Optional tuning**

Settings → Secrets and variables → Actions → Variables. Changes take effect on the next run, with no commit needed:

| Variable | Default | What it does |
|---|---|---|
| `POLL_CYCLES` | `1` | Polls per run for the restock accounts (`3` = every ~20s) |
| `POLL_MODE` | `clock` | `clock` spreads polls evenly across the minute; `window` is the original timing |
| `NITTER_BASE` | two public instances | Comma-separated Nitter instances for the fallback, tried in order |
| `FEED_SOURCE` | `auto` | `auto` (FxTwitter, then Nitter), `fx`, or `nitter` |
| `DISCORD_ACCOUNT_IDENTITY` | `1` | `0` posts under the webhook's own name and avatar instead of the account's |
| `DISCORD_RESTOCK_ROLE_ID` | — | A role to mention on fresh restock posts, so members get notified |

---

## Project Structure

```
silph-relay/
├── src/
│   ├── main.py           # Orchestrates a run: polls, posts, records, reports health
│   ├── fetcher.py        # Pulls posts from FxTwitter, falls back to Nitter
│   ├── discord_poster.py # Sends posts to Discord via webhook
│   ├── tracker.py        # Loads and saves the data files
│   ├── commit_data.py    # Publishes the data files back to the repo, safely
│   ├── heartbeat.py      # Reports each run to healthchecks.io (optional)
│   ├── watchdog.py       # Cancels stuck relay runs; tests the fallback hourly
│   ├── net.py            # Small standard-library HTTP helpers
│   └── envfile.py        # Loads .env for local runs
├── data/
│   ├── seen_ids.json     # Every post that has been relayed
│   ├── deliveries.json   # When each post reached Discord (for the dashboard)
│   └── recent_posts.json # The last 200 relayed posts (for the dashboard)
├── .github/workflows/
│   ├── pipeline.yml      # The relay (dispatch-triggered, every minute)
│   └── watchdog.yml      # The watchdog (dispatch-triggered, every 5 minutes)
├── dashboard/
│   └── index.html        # Feed analytics dashboard (static, deployed to Netlify)
├── netlify.toml
└── .env.example
```

---

## Dashboard

`dashboard/index.html` is a static analytics page (deployed to Netlify) showing posting
activity per account, bucketed by week or month, a weekday × hour activity heatmap, how
quickly posts reach Discord, and a live relay-status chip.

It is **completely decoupled from the relay**: the page runs entirely in the viewer's
browser and reads the published data files over GitHub's CDN, plus the relay's latest
successful run from GitHub's public API. There is no build step, no server, no scheduled job, and nothing that writes to this repo — so a
dashboard refresh can never delay or collide with a posting run.

Every statistic is derived from the data files themselves. The account handle sits in each post's
URL, and the numeric post ID is a [Snowflake](https://en.wikipedia.org/wiki/Snowflake_ID)
whose high bits are the exact creation time (`created_ms = (id >> 22) + 1288834974657`),
so no API calls are needed.

## Not posting the same thing twice

`seen_ids.json` stores the ID of every post that has been relayed. Since GitHub Actions has no persistent filesystem between runs, the workflow commits this file back to the repo after each run (tagged `[skip ci]` to prevent loops). Runs never overlap, and each one checks out the branch as the previous run left it — not the older commit it was dispatched at — so posts already relayed are always skipped.

Matching is on the numeric post ID rather than the URL, so a post still counts as already-relayed if the link form changes. If two tracked accounts surface the same post, even in the same minute, it's relayed once, under the account that wrote it.

---

*Silph Relay is a fan-made tool and is not affiliated with Niantic, The Pokémon Company, or any of the accounts it monitors.*
