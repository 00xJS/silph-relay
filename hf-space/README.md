---
title: silph-rsshub
emoji: 🔁
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# silph-rsshub

Self-hosted [RSSHub](https://github.com/DIYgod/RSSHub) instance backing the
[silph-relay](https://github.com/00xJS/silph-relay) pipeline (X → Discord relay).

## Required Space settings (Settings → Variables and secrets)

Secrets (encrypted):
- `TWITTER_AUTH_TOKEN` — comma-separated `auth_token` cookies from burner X accounts (2–3 recommended; RSSHub rotates and auto-locks them)
- `ACCESS_KEY` — random string; every feed request must carry `?key=<value>`, which keeps strangers from using this public Space and burning the tokens

Variables (plain):
- `CACHE_EXPIRE` = `60` — route cache in seconds (default 300 is too stale for this pipeline; keep the cache ON — token-safety locks live in it)

The consuming pipeline polls every few minutes, which keeps this Space awake
permanently (free CPU Spaces only sleep after 48 hours without traffic).
