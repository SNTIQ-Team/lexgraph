#!/usr/bin/env bash
# Daily realtime retrieval: refresh the Lexgraph dataset from the live
# sources, then push the web data plane to production (api.sntiq.com/lex)
# and restart the API (it caches the data in-process at startup).
#
# Runs on this workstation via the lexgraph-refresh systemd USER timer
# (~/.config/systemd/user/lexgraph-refresh.{service,timer}) — the 1 GB
# production VPS cannot run the pipeline itself.
set -euo pipefail
cd "$(dirname "$0")/.."

./refresh.sh

echo "==> push web data -> sntiq:/srv/sntiq-lexapi/data/web-data"
rsync -az --delete web/data/ sntiq:/srv/sntiq-lexapi/data/web-data/
ssh sntiq 'chown -R http:http /srv/sntiq-lexapi/data/web-data && systemctl restart sntiq-lexapi && sleep 3 && systemctl is-active sntiq-lexapi'

built=$(curl -fsS https://api.sntiq.com/lex/health \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["built_at"])')
echo "OK — live built_at: $built"
