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

# Serialize manual/user-timer runs on this host.  Publication also takes the
# production /run/lock lock below; a local lock alone cannot serialize with a
# timer running on the VPS.
LOCK_FILE="${LEXGRAPH_REFRESH_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/lexgraph-refresh.lock}"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "refresh-and-push: another Lexgraph refresh holds $LOCK_FILE; skipping"
    exit 0
fi

./refresh.sh

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
remote_stage="/srv/sntiq-lexapi/data/web-data.incoming-$stamp"
cleanup_remote() {
    ssh sntiq "rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

echo "==> upload complete generation -> sntiq:$remote_stage"
rsync -az --delete web/data/ "sntiq:$remote_stage/"

echo "==> acquire production lock + atomic publish"
ssh sntiq "flock -w 1800 /run/lock/lexgraph-refresh.lock \
  /srv/sntiq-lexgraph/deploy/publish-web-data.sh '$remote_stage'"
cleanup_remote
trap - EXIT

built=$(curl -fsS https://api.sntiq.com/lex/health \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["built_at"])')
echo "OK — live built_at: $built"
