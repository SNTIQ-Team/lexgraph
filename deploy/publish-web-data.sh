#!/usr/bin/env bash
# Validate and atomically publish one complete Lexgraph API data generation.
#
# The API reads LEXGRAPH_DATA=/srv/sntiq-lexapi/data/web-data.  That path is a
# symlink to an immutable release directory, so readers can see either the old
# generation or the new one, never a half-written rsync.  The first invocation
# migrates the legacy real directory while the API is stopped for a few
# milliseconds.  A failed restart rolls the link/directory back.
set -euo pipefail

SRC="${1:-web/data}"
ROOT="${LEXGRAPH_API_DATA_ROOT:-/srv/sntiq-lexapi/data}"
TARGET="$ROOT/web-data"
SERVICE="${LEXGRAPH_API_SERVICE:-sntiq-lexapi}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE="$ROOT/web-data.release-${STAMP}-$$"
NEXT="$ROOT/.web-data.next-$$"
PUBLISHED=0

cleanup() {
    rm -f -- "$NEXT"
    if [ "$PUBLISHED" -eq 0 ] && [ -d "$RELEASE" ]; then
        rm -rf -- "$RELEASE"
    fi
}
trap cleanup EXIT

test -d "$SRC"
mkdir -p "$RELEASE"
rsync -a --delete "$SRC/" "$RELEASE/"

PYTHON="${LEXGRAPH_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x /srv/sntiq-lexgraph/venv/bin/python ]; then
        PYTHON=/srv/sntiq-lexgraph/venv/bin/python
    else
        PYTHON=python3
    fi
fi

"$PYTHON" - "$RELEASE" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
required = (
    "summary.json", "wiki.json", "hierarchy.json", "graph.json",
    "git.json", "watched_procedures.json", "amendment_fates.json",
    "search.sqlite",
)
for name in required:
    path = root / name
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"publish validation: missing/empty {name}")
for name in required[:-1]:
    with (root / name).open(encoding="utf-8") as handle:
        json.load(handle)
with sqlite3.connect(f"file:{root / 'search.sqlite'}?mode=ro", uri=True) as db:
    result = db.execute("PRAGMA quick_check").fetchone()
if not result or result[0] != "ok":
    raise SystemExit(f"publish validation: sqlite quick_check={result!r}")
if not any((root / "acts").glob("*.json")):
    raise SystemExit("publish validation: acts directory is empty")
PY

chown -R http:http "$RELEASE"

old_link=""
bootstrap=""
if [ -L "$TARGET" ]; then
    old_link="$(readlink "$TARGET")"
    ln -s "$(basename "$RELEASE")" "$NEXT"
    mv -Tf "$NEXT" "$TARGET"
else
    # One-time migration from the old mutable directory.  Stop only for the
    # two atomic metadata operations; the fully validated release already
    # exists before this point.
    systemctl stop "$SERVICE"
    if [ -e "$TARGET" ]; then
        bootstrap="$ROOT/web-data.bootstrap-${STAMP}"
        mv -T "$TARGET" "$bootstrap"
    fi
    ln -s "$(basename "$RELEASE")" "$TARGET"
fi
PUBLISHED=1

rollback() {
    echo "publish failed: rolling back $TARGET" >&2
    if [ -n "$old_link" ]; then
        ln -s "$old_link" "$NEXT"
        mv -Tf "$NEXT" "$TARGET"
    elif [ -n "$bootstrap" ]; then
        rm -f -- "$TARGET"
        mv -T "$bootstrap" "$TARGET"
    fi
    systemctl restart "$SERVICE" || true
}

# A successful systemctl return is not enough: uvicorn can still die during
# import/startup.  Treat delayed liveness or health failure exactly like a
# restart failure and restore the prior immutable generation.
if ! (systemctl restart "$SERVICE" \
      && sleep 3 \
      && systemctl is-active --quiet "$SERVICE" \
      && curl -fsS http://127.0.0.1:8002/health >/dev/null); then
    rollback
    exit 1
fi

# Retain three immutable generations for instant manual rollback.  The legacy
# bootstrap is kept separately until an operator chooses to remove it.
mapfile -t releases < <(
    find "$ROOT" -maxdepth 1 -mindepth 1 -type d \
        -name 'web-data.release-*' -printf '%T@ %p\n' \
        | sort -rn | cut -d' ' -f2-
)
if [ "${#releases[@]}" -gt 3 ]; then
    for old in "${releases[@]:3}"; do
        [ "$old" = "$RELEASE" ] || rm -rf -- "$old"
    done
fi

echo "published $(basename "$RELEASE") -> $TARGET"
