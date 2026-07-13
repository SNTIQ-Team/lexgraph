#!/usr/bin/env bash
# Daily realtime retrieval ON the production VPS (systemd:
# lexgraph-refresh.timer, 04:37 UTC). Runs the full pipeline (peak RSS
# ~100 MB — fits the 1 GB host at idle-priority), publishes the fresh
# web data plane to the API and restarts it (data is cached in-process).
#
# OPENROUTER_API_KEY (via /etc/sntiq/lexgraph.env) enables the LLM
# digest step inside refresh.sh; without it the digest is skipped.
set -euo pipefail
cd "$(dirname "$0")/.."

./refresh.sh

echo "==> publish web data -> /srv/sntiq-lexapi/data/web-data"
rsync -a --delete web/data/ /srv/sntiq-lexapi/data/web-data/
chown -R http:http /srv/sntiq-lexapi/data/web-data
systemctl restart sntiq-lexapi
sleep 3
systemctl is-active sntiq-lexapi

built=$(curl -fsS http://127.0.0.1:8002/health \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["built_at"])')
echo "OK — live built_at: $built"
