# Hugging Face Space: RSSHub instance backing silph-relay.
# Upload this file + README.md to a new Docker Space, then set the
# secrets listed in README.md under the Space's Settings.
FROM diygod/rsshub:latest

# HF Spaces routes inbound traffic to app_port (7860, declared in README.md);
# RSSHub reads its listen port from PORT.
ENV PORT=7860
EXPOSE 7860
