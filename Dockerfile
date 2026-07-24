# Co-located RSSHub + silph-relay for a single Render free web service.
# Public $PORT serves /health; RSSHub listens on localhost:1200.
#
# Base image ships a working RSSHub; we add Python + our relay on top.
# RSSHub needs TWITTER_AUTH_TOKEN (one burner account's auth_token cookie)
# set as an env var on the service for its twitter routes to work.

FROM diygod/rsshub:latest

USER root

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       python3 python3-pip python3-venv curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /relay

COPY requirements.txt /relay/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /relay/requirements.txt \
  || pip3 install --no-cache-dir -r /relay/requirements.txt

COPY src/ /relay/src/
COPY data/ /relay/data/
COPY start.sh /relay/start.sh
RUN chmod +x /relay/start.sh

ENV RSSHUB_URL=http://127.0.0.1:1200 \
    RSSHUB_PORT=1200 \
    CACHE_EXPIRE=30 \
    POLL_INTERVAL_SECONDS=45 \
    SWEEP_INTERVAL_SECONDS=300 \
    NODE_ENV=production \
    PORT=10000

# Render injects PORT at runtime; EXPOSE is documentation only
EXPOSE 10000

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -sf "http://127.0.0.1:${PORT:-10000}/health" || exit 1

CMD ["/relay/start.sh"]
