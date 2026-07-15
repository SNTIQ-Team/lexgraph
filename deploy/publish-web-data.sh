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
import re
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
required = (
    "summary.json", "wiki.json", "hierarchy.json", "graph.json",
    "git.json", "watched_procedures.json", "amendment_fates.json",
    "verified_federal_events.json", "gii_catalog.json", "data_policy.json",
    "search.sqlite",
)
for name in required:
    path = root / name
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"publish validation: missing/empty {name}")
for name in required:
    if not name.endswith(".json"):
        continue
    with (root / name).open(encoding="utf-8") as handle:
        json.load(handle)
with (root / "summary.json").open(encoding="utf-8") as handle:
    summary = json.load(handle)
with (root / "gii_catalog.json").open(encoding="utf-8") as handle:
    catalog = json.load(handle)
with (root / "data_policy.json").open(encoding="utf-8") as handle:
    policy = json.load(handle)
with (root / "graph.json").open(encoding="utf-8") as handle:
    graph = json.load(handle)
if int(summary.get("gii_catalog_total") or 0) != int(catalog.get("total") or 0):
    raise SystemExit("publish validation: GII catalogue total mismatch")
if policy.get("includes_quarantined_sources") or not policy.get("public_build"):
    raise SystemExit("publish validation: refusing non-public/quarantined data build")
graph_policy = graph.get("source_policy") or {}
if graph_policy.get("includes_quarantined_sources") or not graph_policy.get(
        "public_build"):
    raise SystemExit("publish validation: graph source policy is not public")
for path in (root / "feed.json", root / "graph.json",
             root / "verified_federal_events.json"):
    folded = path.read_text(encoding="utf-8").casefold()
    if any(marker in folded for marker in (
            '"source":"buzer"', "buzer.de", '"source":"parlamentsspiegel"',
            "länder-monitor")):
        raise SystemExit(
            f"publish validation: quarantined source leaked into {path.name}")
for path in (root / "acts").glob("*.json"):
    act = json.loads(path.read_text(encoding="utf-8"))
    cross_checks = act.pop("cross_checks", [])
    for row in cross_checks:
        if (not isinstance(row, dict)
                or row.get("source") != "buzer"
                or row.get("authoritative") is not False
                or not re.fullmatch(
                    r"https://www\.buzer\.de/gesetz/[1-9][0-9]*/l\.htm",
                    str(row.get("url") or ""))):
            raise SystemExit(
                f"publish validation: invalid external cross-check in {path.name}")
    folded = json.dumps(act, ensure_ascii=False,
                        separators=(",", ":")).casefold()
    if any(marker in folded for marker in (
            '"source":"buzer"', "buzer.de", '"source":"parlamentsspiegel"',
            "länder-monitor")):
        raise SystemExit(
            f"publish validation: quarantined source leaked into {path.name}")
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
# import/startup.  Warming the 61 MB search index takes about eight seconds on
# the 1 GB VPS, so poll with a hard deadline instead of treating a healthy cold
# start as a failure after a fixed three-second sleep.
healthy=0
if systemctl restart "$SERVICE"; then
    for _attempt in $(seq 1 30); do
        if systemctl is-active --quiet "$SERVICE" \
                && curl -fsS http://127.0.0.1:8002/health >/dev/null 2>&1; then
            healthy=1
            break
        fi
        systemctl is-failed --quiet "$SERVICE" && break
        sleep 1
    done
fi
if [ "$healthy" -ne 1 ]; then
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
