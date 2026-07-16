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
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

root = Path(sys.argv[1])
required = (
    "summary.json", "wiki.json", "hierarchy.json", "graph.json",
    "git.json", "watched_procedures.json", "amendment_fates.json",
    "verified_federal_events.json", "gii_catalog.json", "data_policy.json",
    "official_federal_states.json", "official_transition_reviews.json",
    "verified_reconstructions.json",
    "retrospective_history.json", "retrospective_history.sqlite",
    "federal_states/manifest.json", "search.sqlite",
    "citations.json", "citations.sqlite",
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
with (root / "citations.json").open(encoding="utf-8") as handle:
    citations = json.load(handle)
if int(summary.get("gii_catalog_total") or 0) != int(catalog.get("total") or 0):
    raise SystemExit("publish validation: GII catalogue total mismatch")
if policy.get("includes_quarantined_sources") or not policy.get("public_build"):
    raise SystemExit("publish validation: refusing non-public/quarantined data build")
graph_policy = graph.get("source_policy") or {}
if graph_policy.get("includes_quarantined_sources") or not graph_policy.get(
        "public_build"):
    raise SystemExit("publish validation: graph source policy is not public")

# Citation rows stay in an immutable, exact-indexed SQLite artifact so the API
# never deserializes a 30+ MB JSON graph on the memory-constrained host.  The
# JSON sibling is metadata only and must agree with both summary and database.
citation_counts = citations.get("counts") or {}
citation_storage = citations.get("storage") or {}
citation_policy = citations.get("source_policy") or {}
if citations.get("schema_version") != 1 or \
        citations.get("machine_extracted") is not True or \
        citations.get("current_state_only") is not True or \
        citations.get("legal_interpretation") != "not_asserted" or \
        "citations" in citations or \
        citation_storage != {
            "format": "sqlite3", "file": "citations.sqlite",
            "table": "citation", "rows": citation_counts.get("total"),
            "ordering": "ordinal", "read_only": True,
        } or citation_policy.get("official_current_text_only") is not True or \
        citation_policy.get("fuzzy_matching") is not False or \
        citation_policy.get("cross_act_resolution") != \
        "exact_explicit_alias_only" or summary.get("citations") != \
        citation_counts:
    raise SystemExit("publish validation: invalid citation manifest")
with sqlite3.connect(
        f"file:{root / 'citations.sqlite'}?mode=ro", uri=True) as db:
    citation_check = db.execute("PRAGMA quick_check").fetchone()
    citation_tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    citation_indexes = {row[1] for row in db.execute(
        "PRAGMA index_list('citation')")}
    citation_version = db.execute(
        "SELECT value FROM citation_meta WHERE key='schema_version'"
    ).fetchone()
    citation_meta_counts = db.execute(
        "SELECT value FROM citation_meta WHERE key='counts'"
    ).fetchone()
    citation_db_counts = {
        "total": db.execute("SELECT COUNT(*) FROM citation").fetchone()[0],
        "resolved": db.execute(
            "SELECT COUNT(*) FROM citation WHERE status='resolved'").fetchone()[0],
        "unresolved": db.execute(
            "SELECT COUNT(*) FROM citation WHERE status='unresolved'").fetchone()[0],
        "self": db.execute(
            "SELECT COUNT(*) FROM citation WHERE kind='self'").fetchone()[0],
        "cross_act": db.execute(
            "SELECT COUNT(*) FROM citation WHERE kind='cross_act'").fetchone()[0],
    }
    invalid_citations = db.execute("""
        SELECT COUNT(*) FROM citation
        WHERE machine_extracted != 1 OR current_state_only != 1
           OR legal_interpretation != 'not_asserted'
           OR date_basis !=
              'current_consolidated_snapshot_observation_not_legal_effect'
           OR source_snapshot IS NULL
           OR (status = 'resolved' AND
               (unresolved_reason IS NOT NULL OR target_act IS NULL))
           OR (status = 'unresolved' AND unresolved_reason IS NULL)
    """).fetchone()[0]
if not citation_check or citation_check[0] != "ok" or not {
        "citation_meta", "citation"} <= citation_tables or \
        not {"citation_source_act", "citation_source_jurabk",
             "citation_target_act", "citation_target_jurabk",
             "citation_target_pinpoint"} <= citation_indexes or \
        not citation_version or citation_version[0] != "1" or \
        not citation_meta_counts or json.loads(citation_meta_counts[0]) != \
        citation_counts or citation_db_counts != {
            key: int(citation_counts.get(key) or -1)
            for key in citation_db_counts
        } or invalid_citations:
    raise SystemExit(
        "publish validation: citation sqlite integrity/count mismatch")

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
official_state_projection = {}
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
            state.get("norm_count") != len(state["norms"]) or any(
                not isinstance(norm, dict) or set(norm) != {
                    "enbez", "titel", "text", "glied"} or any(
                        not isinstance(norm.get(field), str)
                        for field in ("enbez", "titel", "text", "glied"))
                for norm in state["norms"]):
        raise SystemExit(
            f"publish validation: invalid federal state shape {digest}")
    # Retain only the identity tuple after verification.  Full parsed states
    # can be tens of megabytes; keeping all of them alive would needlessly
    # pressure the small production host during an otherwise atomic publish.
    loaded_states[digest] = (
        state["id"], state["jurabk"], state["norm_count"])
    official_state_projection[digest] = tuple(
        state[field] for field in (
            "id", "jurabk", "juris", "title", "stand", "build"))

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
    if bool(review.get("retroactive")) != (effective < published) or \
            previous_observed > observed:
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

# Reviewed reverse replays may add derived full-state objects beside the
# official GII CAS.  They are deliberately absent from the official manifest:
# publication accepts only objects named by this separate artifact, re-hashes
# their deterministic gzip and canonical JSON, and requires an official GII
# anchor plus an explicit source_exact=false evidence boundary.
with (root / "verified_reconstructions.json").open(
        encoding="utf-8") as handle:
    verified_reconstructions = json.load(handle)
if verified_reconstructions.get("schema_version") != 1 or \
        verified_reconstructions.get("kind") != \
        "lexgraph-reviewed-verified-reconstructions" or \
        verified_reconstructions.get("state_identity") != \
        "sha256-canonical-uncompressed-json":
    raise SystemExit(
        "publish validation: unsupported verified reconstruction artifact")
try:
    reconstruction_built = datetime.fromisoformat(
        str(verified_reconstructions.get("built_at")).replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(
        "publish validation: invalid verified reconstruction built_at")
if reconstruction_built.tzinfo is None:
    raise SystemExit(
        "publish validation: verified reconstruction built_at lacks timezone")
reconstruction_rows = verified_reconstructions.get("reconstructions")
derived_objects = verified_reconstructions.get("object_metadata")
if not isinstance(reconstruction_rows, list) or \
        not isinstance(derived_objects, dict):
    raise SystemExit(
        "publish validation: malformed verified reconstruction artifact")

derived_loaded_states = {}
for digest, metadata in derived_objects.items():
    if not digest_re.fullmatch(str(digest)) or not isinstance(metadata, dict):
        raise SystemExit(
            "publish validation: malformed derived state metadata")
    anchor = str(metadata.get("anchor_state_sha256") or "")
    expected = Path("objects") / "sha256" / digest[:2] / \
        f"{digest}.json.gz"
    if digest in objects or metadata.get("path") != expected.as_posix() or \
            metadata.get("state_sha256") != digest or \
            metadata.get("origin") != \
            "derived_verified_reverse_replay" or \
            metadata.get("source_exact") is not False or \
            anchor not in loaded_states or anchor == digest:
        raise SystemExit(
            f"publish validation: unsafe derived state metadata {digest}")
    path = root / "federal_states" / expected
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(
            f"publish validation: missing derived state object {digest}")
    compressed = path.read_bytes()
    if len(compressed) < 18 or compressed[:4] != b"\x1f\x8b\x08\x00" or \
            compressed[4:8] != b"\x00\x00\x00\x00" or \
            len(compressed) != metadata.get("gzip_bytes") or \
            hashlib.sha256(compressed).hexdigest() != \
            metadata.get("gzip_sha256"):
        raise SystemExit(
            f"publish validation: derived gzip mismatch {digest}")
    try:
        canonical = gzip.decompress(compressed)
        state = json.loads(canonical.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"publish validation: unreadable derived state {digest}: {exc}")
    encoded = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    if encoded != canonical or len(canonical) != \
            metadata.get("canonical_bytes") or \
            hashlib.sha256(canonical).hexdigest() != digest or \
            not isinstance(state, dict) or set(state) != {
                "id", "jurabk", "juris", "title", "stand", "build",
                "norm_count", "norms"} or state.get("juris") != "DE" or \
            not str(state.get("id") or "").startswith("fed_") or \
            not isinstance(state.get("norms"), list) or \
            state.get("norm_count") != len(state["norms"]):
        raise SystemExit(
            f"publish validation: canonical derived state mismatch {digest}")
    projection = tuple(state[field] for field in (
        "id", "jurabk", "juris", "title", "stand", "build"))
    if projection != official_state_projection[anchor]:
        raise SystemExit(
            f"publish validation: derived anchor projection drift {digest}")
    derived_loaded_states[digest] = (
        state["id"], state["jurabk"], state["norm_count"])

reconstruction_ids = set()
reconstruction_intervals = set()
reconstruction_digests = set()
for row in reconstruction_rows:
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or \
            not row["id"] or row["id"] in reconstruction_ids:
        raise SystemExit(
            "publish validation: malformed/duplicate reconstruction")
    reconstruction_ids.add(row["id"])
    digest = str(row.get("state_sha256") or "")
    anchor = str(row.get("anchor_state_sha256") or "")
    if digest not in derived_loaded_states or \
            derived_objects[digest].get("anchor_state_sha256") != anchor or \
            anchor not in loaded_states or \
            row.get("act_id") != derived_loaded_states[digest][0] or \
            row.get("jurabk") != derived_loaded_states[digest][1] or \
            row.get("act_id") != loaded_states[anchor][0] or \
            row.get("jurabk") != loaded_states[anchor][1] or \
            row.get("text_status") != "derived_verified" or \
            row.get("body_complete") is not True or \
            row.get("source_exact") is not False or \
            row.get("reverse_replay_verified") is not True or \
            row.get("anchor_projection_metadata_retained") is not True or \
            row.get("date_status") != "official_verified" or \
            row.get("date_basis") != \
            "official_bgbl_dip_boundaries_and_verified_replay" or \
            row.get("verification") != \
            "exact_cardinality_inverse_and_canonical_forward_replay" or \
            not isinstance(row.get("changes_reversed"), list) or \
            not row["changes_reversed"] or row.get("gaps") != []:
        raise SystemExit(
            f"publish validation: reconstruction crossed evidence boundary {digest}")
    try:
        effective_from = date.fromisoformat(str(row.get("effective_from")))
        effective_to = date.fromisoformat(str(row.get("effective_to")))
        published_at = date.fromisoformat(str(row.get("published_at")))
        date.fromisoformat(str(row.get("observed_at")))
        knowledge_from = datetime.fromisoformat(
            str(row.get("knowledge_from")).replace("Z", "+00:00"))
        knowledge_to = (datetime.fromisoformat(
            str(row["knowledge_to"]).replace("Z", "+00:00"))
            if row.get("knowledge_to") else None)
    except ValueError:
        raise SystemExit(
            f"publish validation: invalid reconstruction date {digest}")
    if effective_to <= effective_from or knowledge_from.tzinfo is None or \
            (knowledge_to and knowledge_to.tzinfo is None) or \
            (knowledge_to and knowledge_to <= knowledge_from) or \
            bool(row.get("retroactive")) != (effective_from < published_at):
        raise SystemExit(
            f"publish validation: impossible reconstruction interval {digest}")
    incoming = row.get("incoming_event") or {}
    outgoing = row.get("outgoing_event") or {}
    if incoming.get("effective_at") != row.get("effective_from") or \
            outgoing.get("effective_at") != row.get("effective_to") or \
            incoming.get("published_at") != row.get("published_at"):
        raise SystemExit(
            f"publish validation: reconstruction boundary mismatch {digest}")
    evidence = row.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise SystemExit(
            f"publish validation: reconstruction lacks evidence {digest}")
    evidence_sources = set()
    for item in evidence:
        source = item.get("source") if isinstance(item, dict) else None
        parsed = urlparse(str(item.get("url") or "")) \
            if isinstance(item, dict) else None
        if source not in allowed_evidence or parsed.scheme != "https" or \
                parsed.hostname not in allowed_evidence[source]:
            raise SystemExit(
                "publish validation: reconstruction has non-official evidence")
        evidence_sources.add(source)
        if source == "GII" and item.get("state_sha256") != anchor:
            raise SystemExit(
                "publish validation: reconstruction GII anchor mismatch")
    if evidence_sources != set(allowed_evidence):
        raise SystemExit(
            "publish validation: reconstruction evidence set is incomplete")
    reconstruction_digests.add(digest)
    interval_key = (
        row["act_id"], digest, row["effective_from"], row["effective_to"])
    if interval_key in reconstruction_intervals:
        raise SystemExit(
            "publish validation: duplicate reconstruction interval")
    reconstruction_intervals.add(interval_key)
if reconstruction_digests != set(derived_objects) or \
        int(summary.get("verified_reconstructions") or 0) != \
        len(reconstruction_rows):
    raise SystemExit(
        "publish validation: reconstruction object/count mismatch")

# No orphan, stale or partial file may piggy-back on the CAS directory.  The
# exact allow-list is the official manifest plus the reviewed-derived artifact.
expected_cas_paths = {
    str(metadata["path"]) for metadata in objects.values()
} | {
    str(metadata["path"]) for metadata in derived_objects.values()
}
cas_root = root / "federal_states" / "objects"
actual_cas_paths = set()
for path in cas_root.rglob("*"):
    if path.is_symlink():
        raise SystemExit(
            f"publish validation: symlink in federal state CAS: {path.name}")
    if path.is_file():
        actual_cas_paths.add(path.relative_to(
            root / "federal_states").as_posix())
    elif not path.is_dir():
        raise SystemExit(
            f"publish validation: special file in federal state CAS: {path.name}")
if actual_cas_paths != expected_cas_paths:
    raise SystemExit("publish validation: unreviewed federal CAS extras")
all_loaded_states = {**loaded_states, **derived_loaded_states}

# The public retrospective manifest adds legal-valid time and Lexgraph
# knowledge time without copying state bodies.  Every body reference must be
# one of the canonical GII objects verified above.  Event-only BGBl rows are
# allowed, but may never claim that a historical consolidated body exists.
with (root / "retrospective_history.json").open(encoding="utf-8") as handle:
    retrospective = json.load(handle)
if retrospective.get("schema_version") != 1 or retrospective.get("kind") != \
        "lexgraph-retrospective-history":
    raise SystemExit("publish validation: unsupported retrospective history")
retro_policy = retrospective.get("source_policy") or {}
if retro_policy.get("official_only") is not True or \
        retro_policy.get("effective_dates_inferred") is not False or \
        retro_policy.get("buzer_input") is not False:
    raise SystemExit("publish validation: invalid retrospective source policy")
expected_retrospective_objects = {**objects, **derived_objects}
if retrospective.get("objects") != expected_retrospective_objects:
    raise SystemExit("publish validation: retrospective state catalogue drift")
try:
    retrospective_built = datetime.fromisoformat(
        str(retrospective.get("built_at")).replace("Z", "+00:00"))
except ValueError:
    raise SystemExit("publish validation: invalid retrospective built_at")
if retrospective_built.tzinfo is None:
    raise SystemExit("publish validation: retrospective built_at lacks timezone")
retro_acts = retrospective.get("acts")
if not isinstance(retro_acts, dict) or not retro_acts:
    raise SystemExit("publish validation: retrospective acts are empty")
retro_interval_count = retro_event_count = retro_observation_count = 0
seen_reconstruction_intervals = set()
for act_id, act in retro_acts.items():
    if not isinstance(act, dict) or act.get("act_id") != act_id:
        raise SystemExit("publish validation: malformed retrospective act")
    intervals = act.get("intervals")
    events = act.get("events")
    observations_for_act = act.get("observations")
    if not isinstance(intervals, list) or not isinstance(events, list) or \
            not isinstance(observations_for_act, list):
        raise SystemExit("publish validation: malformed retrospective rows")
    retro_interval_count += len(intervals)
    retro_event_count += len(events)
    retro_observation_count += len(observations_for_act)
    for row in intervals:
        digest = str(row.get("state_sha256") or "")
        if digest not in all_loaded_states or \
                all_loaded_states[digest][0] != act_id:
            raise SystemExit(
                "publish validation: retrospective interval state mismatch")
        try:
            effective_from = date.fromisoformat(str(row.get("effective_from")))
            effective_to = (date.fromisoformat(str(row["effective_to"]))
                            if row.get("effective_to") else None)
            knowledge_from = datetime.fromisoformat(
                str(row.get("knowledge_from")).replace("Z", "+00:00"))
            knowledge_to = (datetime.fromisoformat(
                str(row["knowledge_to"]).replace("Z", "+00:00"))
                if row.get("knowledge_to") else None)
        except ValueError:
            raise SystemExit(
                "publish validation: invalid retrospective interval date")
        if knowledge_from.tzinfo is None or (knowledge_to and
                knowledge_to.tzinfo is None) or (effective_to and
                effective_to <= effective_from) or (knowledge_to and
                knowledge_to <= knowledge_from):
            raise SystemExit(
                "publish validation: impossible retrospective interval")
        published = (date.fromisoformat(str(row["published_at"]))
                     if row.get("published_at") else None)
        if bool(row.get("retroactive")) != bool(
                published and effective_from < published):
            raise SystemExit(
                "publish validation: interval retroactive marker mismatch")
        if digest in derived_loaded_states:
            provenance = row.get("provenance") or {}
            interval_key = (act_id, digest, row.get("effective_from"),
                            row.get("effective_to"))
            if interval_key not in reconstruction_intervals or \
                    row.get("text_status") != "derived_verified" or \
                    row.get("source_exact") is not False or \
                    row.get("body_complete") is not True or \
                    row.get("reverse_replay_verified") is not True or \
                    provenance.get("method") != \
                    "reviewed_inverse_then_canonical_forward_replay" or \
                    provenance.get("anchor_state_sha256") != \
                    derived_objects[digest].get("anchor_state_sha256"):
                raise SystemExit(
                    "publish validation: unsafe derived retrospective interval")
            seen_reconstruction_intervals.add(interval_key)
        elif row.get("text_status") == "derived_verified" or \
                row.get("source_exact") is False:
            raise SystemExit(
                "publish validation: official interval mislabeled as derived")
    for row in events:
        try:
            published = date.fromisoformat(str(row.get("published_at")))
            effective = (date.fromisoformat(str(row["effective_at"]))
                         if row.get("effective_at") else None)
        except ValueError:
            raise SystemExit(
                "publish validation: invalid retrospective event date")
        if row.get("historical_text_reconstructed") is not False or \
                row.get("text_status") != "event_only" or \
                not digest_re.fullmatch(str(row.get("pdf_sha256") or "")) or \
                bool(row.get("retroactive")) != bool(
                    effective and effective < published):
            raise SystemExit(
                "publish validation: unsupported retrospective event claim")
    for row in observations_for_act:
        digest = str(row.get("state_sha256") or "")
        if digest not in loaded_states or loaded_states[digest][0] != act_id or \
                row.get("date_basis") != \
                "retrieval_observation_not_effective_date":
            raise SystemExit(
                "publish validation: retrospective observation mismatch")
retro_counts = retrospective.get("counts") or {}
if int(retro_counts.get("acts") or -1) != len(retro_acts) or \
        int(retro_counts.get("interval_assertions") or -1) != \
        retro_interval_count or int(retro_counts.get("events") or -1) != \
        retro_event_count or int(retro_counts.get("observations") or -1) != \
        retro_observation_count or summary.get("retrospective_history") != \
        retro_counts:
    raise SystemExit("publish validation: retrospective count mismatch")
if seen_reconstruction_intervals != reconstruction_intervals:
    raise SystemExit(
        "publish validation: verified reconstruction interval missing")

with sqlite3.connect(
        f"file:{root / 'retrospective_history.sqlite'}?mode=ro", uri=True) as db:
    result = db.execute("PRAGMA quick_check").fetchone()
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    sqlite_counts = {
        "legal_intervals": db.execute(
            "SELECT COUNT(*) FROM legal_intervals").fetchone()[0],
        "amendment_events": db.execute(
            "SELECT COUNT(*) FROM amendment_events").fetchone()[0],
        "state_observations": db.execute(
            "SELECT COUNT(*) FROM state_observations").fetchone()[0],
    }
if not result or result[0] != "ok" or not {
        "metadata", "acts", "legal_intervals", "amendment_events",
        "state_observations"} <= tables or sqlite_counts != {
            "legal_intervals": retro_interval_count,
            "amendment_events": retro_event_count,
            "state_observations": retro_observation_count,
        }:
    raise SystemExit(
        "publish validation: retrospective sqlite integrity/count mismatch")

for path in root.rglob("*"):
    if "buzer" in path.name.casefold():
        raise SystemExit(
            f"publish validation: quarantined file leaked: {path.name}")
for path in (root / "feed.json", root / "graph.json",
             root / "verified_federal_events.json",
             root / "verified_reconstructions.json",
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
