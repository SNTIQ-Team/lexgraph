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
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

root = Path(sys.argv[1])
required = (
    "summary.json", "wiki.json", "hierarchy.json", "graph.json",
    "git.json", "watched_procedures.json", "amendment_fates.json",
    "verified_federal_events.json", "gii_catalog.json", "data_policy.json",
    "official_federal_states.json", "official_transition_reviews.json",
    "federal_states/manifest.json", "search.sqlite",
)
for name in required:
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
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

# The official federal archive is a self-contained evidence store.  Validate
# every referenced blob before making the release visible: gzip integrity,
# compressed and canonical hashes, canonical encoding, act identity and every
# retrieval observation.  Observation dates must never masquerade as legal
# commencement dates.
with (root / "official_federal_states.json").open(encoding="utf-8") as handle:
    states = json.load(handle)
with (root / "federal_states/manifest.json").open(encoding="utf-8") as handle:
    state_manifest = json.load(handle)
if states != state_manifest:
    raise SystemExit(
        "publish validation: official federal state manifests differ")
if states.get("schema_version") != 1 or states.get("kind") != \
        "lexgraph-official-federal-state-store":
    raise SystemExit("publish validation: unsupported federal state manifest")
if states.get("state_identity") != \
        "sha256-canonical-uncompressed-json" or states.get("compression") != \
        "gzip-mtime-0-empty-filename":
    raise SystemExit("publish validation: unsupported federal state encoding")
objects = states.get("objects")
observations = states.get("observations")
state_policy = states.get("source_policy") or {}
if not isinstance(objects, dict) or not objects or \
        not isinstance(observations, list) or not observations:
    raise SystemExit("publish validation: empty/malformed federal state store")
if not state_policy.get("official_only") or \
        state_policy.get("includes_quarantined_sources") or \
        state_policy.get("source") != "GII" or \
        state_policy.get("date_basis") != \
        "retrieval_observation_not_effective_date" or \
        state_policy.get("effective_dates_inferred") is not False:
    raise SystemExit("publish validation: invalid federal state source policy")
digest_re = re.compile(r"[0-9a-f]{64}")
loaded_states = {}
for digest, metadata in objects.items():
    if not digest_re.fullmatch(str(digest)) or not isinstance(metadata, dict):
        raise SystemExit("publish validation: malformed state object metadata")
    expected = Path("objects") / "sha256" / digest[:2] / \
        f"{digest}.json.gz"
    if metadata.get("path") != expected.as_posix():
        raise SystemExit(
            f"publish validation: unsafe state object path for {digest}")
    path = root / "federal_states" / expected
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(
            f"publish validation: missing state object {digest}")
    compressed = path.read_bytes()
    if len(compressed) < 18 or compressed[:4] != b"\x1f\x8b\x08\x00" or \
            compressed[4:8] != b"\x00\x00\x00\x00":
        raise SystemExit(
            f"publish validation: non-deterministic gzip header {digest}")
    if len(compressed) != metadata.get("gzip_bytes") or \
            hashlib.sha256(compressed).hexdigest() != \
            metadata.get("gzip_sha256"):
        raise SystemExit(
            f"publish validation: compressed state mismatch {digest}")
    try:
        canonical = gzip.decompress(compressed)
        state = json.loads(canonical.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"publish validation: unreadable state {digest}: {exc}")
    encoded = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    if encoded != canonical or len(canonical) != \
            metadata.get("canonical_bytes") or \
            hashlib.sha256(canonical).hexdigest() != digest:
        raise SystemExit(
            f"publish validation: canonical state mismatch {digest}")
    if not isinstance(state, dict) or set(state) != {
            "id", "jurabk", "juris", "title", "stand", "build",
            "norm_count", "norms"} or state.get("juris") != "DE" or \
            not str(state.get("id") or "").startswith("fed_") or \
            not isinstance(state.get("norms"), list) or \
            state.get("norm_count") != len(state["norms"]):
        raise SystemExit(
            f"publish validation: invalid federal state shape {digest}")
    # Retain only the identity tuple after verification.  Full parsed states
    # can be tens of megabytes; keeping all of them alive would needlessly
    # pressure the small production host during an otherwise atomic publish.
    loaded_states[digest] = (
        state["id"], state["jurabk"], state["norm_count"])

observation_keys = set()
for observation in observations:
    if not isinstance(observation, dict):
        raise SystemExit("publish validation: malformed state observation")
    digest = str(observation.get("state_sha256") or "")
    try:
        observed_at = date.fromisoformat(str(observation.get("observed_at")))
    except ValueError:
        raise SystemExit("publish validation: invalid observation date")
    if observed_at.isoformat() != observation.get("observed_at") or \
            digest not in loaded_states or \
            observation.get("date_basis") != \
            "retrieval_observation_not_effective_date" or \
            observation.get("verification") != "exact" or \
            not str(observation.get("source_url") or "").startswith(
                "https://www.gesetze-im-internet.de/"):
        raise SystemExit("publish validation: invalid official observation")
    state_id, state_jurabk, state_norm_count = loaded_states[digest]
    if observation.get("act_id") != state_id or \
            observation.get("jurabk") != state_jurabk or \
            observation.get("norm_count") != state_norm_count:
        raise SystemExit(
            "publish validation: observation/state identity mismatch")
    key = (observation["act_id"], observation["observed_at"], digest,
           str(observation.get("builddate") or ""))
    if key in observation_keys:
        raise SystemExit("publish validation: duplicate state observation")
    observation_keys.add(key)
if int(states.get("total_observations") or -1) != len(observations) or \
        int(states.get("total_states") or -1) != len(objects) or \
        int(summary.get("official_federal_observations") or -1) != \
        len(observations) or \
        int(summary.get("official_federal_states") or -1) != len(objects) or \
        states.get("total_transitions") != \
        summary.get("official_federal_transitions"):
    raise SystemExit("publish validation: federal state count mismatch")

# A reviewed transition is the only place where the archive may claim a legal
# effective date.  It must point back to two captured states and only official
# GII/BGBl/DIP evidence.
with (root / "official_transition_reviews.json").open(
        encoding="utf-8") as handle:
    review_payload = json.load(handle)
review_policy = review_payload.get("source_policy") or {}
reviews = review_payload.get("reviews")
if review_payload.get("schema_version") != 1 or \
        not review_policy.get("official_only") or \
        review_policy.get("includes_quarantined_sources") or \
        review_policy.get("effective_dates_inferred") is not False or \
        not isinstance(reviews, list) or \
        review_payload.get("total") != len(reviews):
    raise SystemExit("publish validation: invalid transition review envelope")
review_ids = set()
allowed_evidence = {
    "GII": {"www.gesetze-im-internet.de"},
    "BGBl": {"www.recht.bund.de"},
    "DIP": {"dip.bundestag.de"},
}
for review in reviews:
    if not isinstance(review, dict) or not isinstance(review.get("id"), str) \
            or review["id"] in review_ids:
        raise SystemExit("publish validation: malformed/duplicate review")
    review_ids.add(review["id"])
    old_digest = str(review.get("previous_state_sha256") or "")
    new_digest = str(review.get("state_sha256") or "")
    if old_digest == new_digest or old_digest not in loaded_states or \
            new_digest not in loaded_states or \
            review.get("act_id") != loaded_states[new_digest][0] or \
            review.get("jurabk") != loaded_states[new_digest][1]:
        raise SystemExit("publish validation: review state pair mismatch")
    if review.get("date_basis") != \
            "official_bgbl_command_and_commencement_clause" or \
            review.get("verification") != \
            "official_final_text_and_complete_state_pair" or \
            not review.get("changes"):
        raise SystemExit("publish validation: review has an unsupported claim")
    try:
        published = date.fromisoformat(str(review.get("published_at")))
        effective = date.fromisoformat(str(review.get("effective_at")))
        observed = date.fromisoformat(str(review.get("observed_at")))
        previous_observed = date.fromisoformat(
            str(review.get("previous_observed_at")))
    except ValueError:
        raise SystemExit("publish validation: review has an invalid date")
    if published > effective or previous_observed > observed:
        raise SystemExit("publish validation: review date order is impossible")
    bgbl = review.get("bgbl") or {}
    if bgbl.get("integrity_verified") is not True or \
            not digest_re.fullmatch(str(bgbl.get("pdf_sha256") or "")):
        raise SystemExit("publish validation: review lacks BGBl integrity")
    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise SystemExit("publish validation: review lacks evidence")
    for item in evidence:
        source = item.get("source") if isinstance(item, dict) else None
        parsed = urlparse(str(item.get("url") or "")) \
            if isinstance(item, dict) else None
        if source not in allowed_evidence or parsed.scheme != "https" or \
                parsed.hostname not in allowed_evidence[source]:
            raise SystemExit(
                "publish validation: review contains non-official evidence")
if int(summary.get("official_federal_legal_reviews") or 0) != len(reviews):
    raise SystemExit("publish validation: transition review count mismatch")

for path in root.rglob("*"):
    if "buzer" in path.name.casefold():
        raise SystemExit(
            f"publish validation: quarantined file leaked: {path.name}")
for path in (root / "feed.json", root / "graph.json",
             root / "verified_federal_events.json",
             root / "official_federal_states.json",
             root / "official_transition_reviews.json"):
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
