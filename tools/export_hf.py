#!/usr/bin/env python3
"""Build the public Lexgraph Hugging Face dataset from the latest snapshots.

The exporter deliberately copies immutable source snapshots instead of the
API's compact presentation JSON wherever possible.  It also exports the two
derived graph views and a merged court-decision table so a dataset consumer
gets the same case set as the public API.

    python3 tools/export_hf.py [--out hf_export]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

from common import latest_snapshot, read_jsonl  # noqa: E402
from official_states import (  # noqa: E402
    DATE_BASIS as OFFICIAL_OBSERVATION_DATE_BASIS,
    canonical_json_bytes,
    load_manifest as load_official_state_manifest,
    load_state_verified as load_official_state,
    transitions as build_official_state_transitions,
)
from api.retrospective_store import validate_manifest  # noqa: E402


# Output name -> (snapshot family, source filename, description)
SNAPSHOT_FILES: dict[str, tuple[str, str, str]] = {
    "patches.jsonl": (
        "patches", "patches.jsonl",
        "legislative PatchInstructions with lifecycle and provenance"),
    "federal_acts.jsonl": (
        "gii", "acts.jsonl", "federal acts in the curated deep corpus"),
    "federal_catalog.jsonl": (
        "gii", "catalog.jsonl",
        "complete official GII table of contents, metadata only"),
    "federal_norms.jsonl": (
        "gii", "norms.jsonl", "current federal sections and full text"),
    "bayern_acts.jsonl": (
        "bayern_recht", "acts.jsonl", "Bavarian acts in the deep corpus"),
    "bayern_norms.jsonl": (
        "bayern_recht", "norms.jsonl", "current Bavarian articles and full text"),
    "bayern_recht_versions.jsonl": (
        "bayern_recht", "versions.jsonl", "official Bavarian amendment metadata"),
    "eu_instruments.jsonl": (
        "eu_layer", "instruments.jsonl", "curated EU instruments with deep links"),
    "eu_transpositions.jsonl": (
        "eu_layer", "transpositions.jsonl", "German implementing measures"),
    "eu_index.jsonl": (
        "eu_index", "instruments.jsonl",
        "all in-force directives and basic regulations, metadata only"),
    "bayern_landtag_bills.jsonl": (
        "bay_landtag", "bills.jsonl", "Bavarian Landtag bills and lifecycle"),
    "bundestag_procedures.jsonl": (
        "dip", "vorgaenge.jsonl",
        "official DIP legislative procedures and current stages"),
    "bgbl_events.jsonl": (
        "bgbl_events", "events.jsonl", "federal promulgation events"),
    "gvbl_events.jsonl": (
        "gvbl_events", "events.jsonl", "Bavarian promulgation events"),
}

DERIVED_FILES: dict[str, tuple[Path, str]] = {
    "bayern_word_diffs.jsonl": (
        ROOT / "data" / "by_diffs.jsonl",
        "verified archived-state and forward daily Bavarian text changes"),
    "git.json": (
        ROOT / "web" / "data" / "git.json",
        "Laws-as-Git event log with commits, open/closed branches and evidence-bound merges"),
    "chronology.json": (
        ROOT / "web" / "data" / "git.json",
        "compatibility alias of git.json for existing dataset consumers"),
    "graph.json": (
        ROOT / "web" / "data" / "graph.json",
        "QFS arena graph export"),
    "watched_procedures.json": (
        ROOT / "web" / "data" / "watched_procedures.json",
        "persistent DIP/EUR-Lex watch state with evidence checks, chronology and qualitative forecasts"),
    "amendment_fates.json": (
        ROOT / "web" / "data" / "amendment_fates.json",
        "reviewed parliamentary document chains and current-law checks"),
    "verified_federal_events.json": (
        ROOT / "web" / "data" / "verified_federal_events.json",
        "official GII state pairs plus explicitly non-historical DIP/current-text correspondences"),
    "data_policy.json": (
        ROOT / "web" / "data" / "data_policy.json",
        "machine-readable exclusions for third-party database rights"),
    "retrospective_history.json": (
        ROOT / "web" / "data" / "retrospective_history.json",
        "bitemporal federal history manifest with separate legal and knowledge time"),
    "retrospective_history.sqlite": (
        ROOT / "web" / "data" / "retrospective_history.sqlite",
        "portable queryable SQLite form of the bitemporal federal history"),
}

# Raw lifecycle files do not exist before the first watch update.  Export them
# when present, while the required watched_procedures.json above always carries
# the complete API-facing state and embedded history.
OPTIONAL_DERIVED_FILES: dict[str, tuple[Path, str]] = {
    "procedure_watch_state.json": (
        ROOT / "data" / "procedure_watch_state.json",
        "raw persistent state used to stop terminal watch polling"),
    "procedure_watch_history.jsonl": (
        ROOT / "data" / "procedure_watch_history.jsonl",
        "append-only official procedure status-change ledger"),
}

OFFICIAL_STATE_STORE = ROOT / "web" / "data" / "federal_states"
OFFICIAL_REVIEWS = ROOT / "web" / "data" / \
    "official_transition_reviews.json"
VERIFIED_RECONSTRUCTIONS = ROOT / "web" / "data" / \
    "verified_reconstructions.json"
NEURIS_ARCHIVE = ROOT / "data" / "neuris_archive.jsonl"
NEURIS_OBJECTS = ROOT / "data" / "neuris_objects"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_NEURIS_OBJECT = re.compile(
    r"neuris_objects/(?P<sha>[0-9a-f]{64})"
    r"(?P<suffix>\.zip|\.xml|\.html)")
_NEURIS_MEDIA = {
    ".zip": "application/zip",
    ".xml": "application/xml",
    ".html": "text/html",
}

OFFICIAL_HISTORY_FILES: dict[str, str] = {
    "official_federal_state_observations.jsonl": (
        "complete official GII retrieval observations; observed_at is not "
        "an effective date"),
    "official_federal_state_transitions.jsonl": (
        "own diffs between adjacent complete GII states, with no inferred "
        "legal date"),
    "official_federal_state_objects.jsonl": (
        "full content-addressed federal act states, including every captured "
        "norm and observation provenance"),
    "official_transition_reviews.jsonl": (
        "state transitions accepted as legal events only after final BGBl "
        "command and commencement review"),
}

RETROSPECTIVE_FILES: dict[str, str] = {
    "retrospective_legal_intervals.jsonl": (
        "verified full-state intervals with effective and knowledge bounds"),
    "retrospective_amendment_events.jsonl": (
        "official 2023+ BGBl amendment events; unresolved dates remain null"),
    "retrospective_observations.jsonl": (
        "complete GII retrieval observations, never reused as legal dates"),
    "retrospective_gaps.jsonl": (
        "explicit missing-date, unreviewed-transition and event-only gaps"),
}


def require_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"missing or empty {label}: {path}")
    return path


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    shutil.copyfile(src, tmp)
    tmp.replace(dst)


def write_bytes_atomic(dst: Path, payload: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.write_bytes(payload)
    tmp.replace(dst)


def file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def copy_verified_file(src: Path, dst: Path, sha256: str, size: int) -> None:
    """Copy one immutable object atomically, reusing a verified destination."""
    if src.is_symlink() or not src.is_file():
        raise RuntimeError(f"missing or unsafe CAS object: {src}")
    actual_hash, actual_size = file_sha256(src)
    if actual_hash != sha256 or actual_size != size:
        raise RuntimeError(f"CAS metadata mismatch: {src}")
    if dst.is_file() and not dst.is_symlink() and \
            file_sha256(dst) == (sha256, size):
        return
    copy_file(src, dst)
    if dst.is_symlink() or file_sha256(dst) != (sha256, size):
        raise RuntimeError(f"copied CAS object failed verification: {dst}")


def line_count(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def case_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("court_short") or "").casefold(),
        re.sub(r"\s+", "", str(row.get("az") or "")).casefold(),
        str(row.get("date") or ""),
    )


def merged_decisions() -> list[dict]:
    """Mirror the API merge: reviewed manual records win duplicates."""
    manual_file = ROOT / "data" / "decisions.json"
    manual = json.loads(require_file(
        manual_file, "reviewed decisions").read_text(encoding="utf-8")
    ).get("decisions") or []

    rii_dir = latest_snapshot("rii")
    automated = list(read_jsonl(require_file(
        rii_dir / "decisions.jsonl" if rii_dir else Path(""),
        "official RII decisions")))

    rows = list(manual)
    ids = {str(row["id"]) for row in rows if row.get("id")}
    cases = {case_key(row) for row in rows}
    for row in automated:
        if (row.get("id") and str(row["id"]) in ids) or case_key(row) in cases:
            continue
        rows.append(row)
        if row.get("id"):
            ids.add(str(row["id"]))
        cases.add(case_key(row))
    return sorted(rows, key=lambda row: row.get("date") or "", reverse=True)


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    tmp.replace(path)
    return count


def _read_official_reviews(path: Path = OFFICIAL_REVIEWS) -> list[dict]:
    """Read the accepted web-data reviews, never a private candidate cache."""
    payload = json.loads(require_file(
        path, "official transition reviews").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("official transition reviews must be an object")
    policy = payload.get("source_policy") or {}
    if not policy.get("official_only") or policy.get(
            "includes_quarantined_sources") or policy.get(
            "effective_dates_inferred") is not False:
        raise RuntimeError(
            "official transition reviews have a non-public source policy")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or payload.get("total") != len(reviews):
        raise RuntimeError("official transition review count mismatch")
    return reviews


def _official_history_rows(
        store: Path = OFFICIAL_STATE_STORE,
        reviews_path: Path = OFFICIAL_REVIEWS) -> dict[str, Iterable[dict]]:
    """Verify and materialize the published GII state store for HF.

    The web generation is the consistency boundary: the exporter reads its
    manifest and CAS, verifies every canonical object, and serializes portable
    JSONL rows instead of leaking local cache paths or gzip implementation
    details into the dataset.
    """
    store = Path(store)
    manifest = load_official_state_manifest(store)
    observations = list(manifest.get("observations") or [])
    objects = manifest.get("objects") or {}
    if not observations or not objects:
        raise RuntimeError("published official federal state store is empty")

    by_digest: dict[str, list[dict]] = {}
    for observation in observations:
        if observation.get("date_basis") != \
                OFFICIAL_OBSERVATION_DATE_BASIS:
            raise RuntimeError("official observation has an invalid date basis")
        by_digest.setdefault(observation["state_sha256"], []).append(
            observation)

    def state_rows() -> Iterable[dict]:
        # Stream full states one at a time.  This keeps HF export memory flat
        # even as the cumulative CAS grows on the small production host.
        for digest in sorted(objects):
            state = load_official_state(store, digest)
            state_observations = sorted(
                by_digest.get(digest, []),
                key=lambda row: (row["observed_at"], row["act_id"]),
            )
            if not state_observations:
                raise RuntimeError(
                    f"official state {digest} has no retrieval observation")
            yield {
                "state_sha256": digest,
                "state_identity": "sha256-canonical-uncompressed-json",
                **state,
                "observations": state_observations,
                "provenance": {
                    "source": "GII",
                    "source_urls": sorted({
                        row["source_url"] for row in state_observations
                    }),
                    "date_basis": OFFICIAL_OBSERVATION_DATE_BASIS,
                    "verification": "exact_complete_state",
                    "effective_date_asserted": False,
                },
            }

    transition_rows = build_official_state_transitions(
        manifest, store)
    for row in transition_rows:
        row["evidence"] = [
            {
                "source": "GII",
                "url": row["source_url"],
                "observed_at": row["previous_observed_at"],
                "state_sha256": row["previous_state_sha256"],
            },
            {
                "source": "GII",
                "url": row["source_url"],
                "observed_at": row["observed_at"],
                "state_sha256": row["state_sha256"],
            },
        ]
        row["provenance"] = {
            "source": "GII",
            "algorithm": "lexgraph-complete-state-diff",
            "date_basis": OFFICIAL_OBSERVATION_DATE_BASIS,
            "effective_date_asserted": False,
            "official_only": True,
        }

    reviews = _read_official_reviews(Path(reviews_path))
    transitions_by_pair = {
        (row["previous_state_sha256"], row["state_sha256"]): row
        for row in transition_rows
    }
    official_hosts = {
        "GII": "www.gesetze-im-internet.de",
        "BGBl": "www.recht.bund.de",
        "DIP": "dip.bundestag.de",
    }
    for review in reviews:
        pair = (review.get("previous_state_sha256"),
                review.get("state_sha256"))
        transition = transitions_by_pair.get(pair)
        if transition is None:
            raise RuntimeError(
                f"official review {review.get('id')} has no state transition")
        if review.get("date_basis") != \
                "official_bgbl_command_and_commencement_clause" or \
                review.get("verification") != \
                "official_final_text_and_complete_state_pair":
            raise RuntimeError(
                f"official review {review.get('id')} has a weak date claim")
        if any(review.get(field) != transition.get(field) for field in (
                "act_id", "jurabk", "observed_at",
                "previous_observed_at")) or not review.get("changes"):
            raise RuntimeError(
                f"official review {review.get('id')} mismatches its state pair")
        bgbl = review.get("bgbl") or {}
        if bgbl.get("integrity_verified") is not True or not re.fullmatch(
                r"[0-9a-f]{64}", str(bgbl.get("pdf_sha256") or "")):
            raise RuntimeError(
                f"official review {review.get('id')} lacks BGBl integrity")
        try:
            published = date.fromisoformat(str(review.get("published_at")))
            effective = date.fromisoformat(str(review.get("effective_at")))
        except ValueError as exc:
            raise RuntimeError(
                f"official review {review.get('id')} has an invalid date") \
                from exc
        # An expressly retroactive commencement is a valid official fact.
        # Preserve and label it rather than enforcing publication <= effect.
        if bool(review.get("retroactive")) != (effective < published):
            raise RuntimeError(
                f"official review {review.get('id')} has an inconsistent "
                "retroactive marker")
        evidence = review.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise RuntimeError(
                f"official review {review.get('id')} lacks evidence")
        for item in evidence:
            source = item.get("source") if isinstance(item, dict) else None
            parsed = urlparse(str(item.get("url") or "")) \
                if isinstance(item, dict) else None
            if source not in official_hosts or parsed.scheme != "https" or \
                    parsed.hostname != official_hosts[source]:
                raise RuntimeError(
                    f"official review {review.get('id')} has private evidence")

    return {
        "official_federal_state_observations.jsonl": observations,
        "official_federal_state_transitions.jsonl": transition_rows,
        "official_federal_state_objects.jsonl": state_rows(),
        "official_transition_reviews.jsonl": reviews,
    }


def _retrospective_rows() -> dict[str, list[dict]]:
    """Flatten the validated public bitemporal manifest for HF viewers."""
    path = require_file(
        ROOT / "web" / "data" / "retrospective_history.json",
        "retrospective history")
    manifest = validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    intervals: list[dict] = []
    events: list[dict] = []
    observations: list[dict] = []
    gaps: list[dict] = []
    for act_id, act in sorted(manifest["acts"].items()):
        identity = {
            "act_id": act_id,
            "jurabk": act.get("jurabk"),
            "act_title": act.get("title"),
        }
        intervals.extend({**identity, **row}
                         for row in act.get("intervals") or [])
        events.extend({**identity, **row}
                      for row in act.get("events") or [])
        observations.extend({**identity, **row}
                            for row in act.get("observations") or [])
        gaps.extend({**identity, **row}
                    for row in act.get("gaps") or [])
        for event in act.get("events") or []:
            gaps.extend({**identity, "event_id": event.get("id"), **row}
                        for row in event.get("gaps") or [])
    return {
        "retrospective_legal_intervals.jsonl": intervals,
        "retrospective_amendment_events.jsonl": events,
        "retrospective_observations.jsonl": observations,
        "retrospective_gaps.jsonl": gaps,
    }


def _sync_exported_objects(root: Path, expected: set[Path]) -> None:
    """Remove stale/partial output objects while retaining verified objects."""
    if root.is_symlink():
        root.unlink()
        return
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts),
                       reverse=True):
        if path.is_symlink():
            path.unlink()
        elif path.is_file() and path.relative_to(root) not in expected:
            path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def _export_verified_reconstructions(
        out: Path, artifact_path: Path = VERIFIED_RECONSTRUCTIONS,
        store: Path = OFFICIAL_STATE_STORE) -> tuple[int, int]:
    """Export reviewed-derived metadata and only its verified CAS objects."""
    artifact_path = require_file(
        Path(artifact_path), "verified reconstructions")
    try:
        artifact_bytes = artifact_path.read_bytes()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("verified reconstructions are invalid JSON") from exc
    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1 \
            or artifact.get("kind") != \
            "lexgraph-reviewed-verified-reconstructions" or \
            artifact.get("state_identity") != \
            "sha256-canonical-uncompressed-json":
        raise RuntimeError("unsupported verified reconstruction artifact")
    rows = artifact.get("reconstructions")
    metadata_by_digest = artifact.get("object_metadata")
    if not isinstance(rows, list) or not isinstance(metadata_by_digest, dict):
        raise RuntimeError("malformed verified reconstruction artifact")

    store = Path(store)
    official_manifest = load_official_state_manifest(store)
    official_objects = official_manifest.get("objects") or {}
    states_by_digest: dict[str, dict] = {}
    expected_paths: set[Path] = set()
    for digest, metadata in metadata_by_digest.items():
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or \
                not isinstance(metadata, dict):
            raise RuntimeError("invalid derived CAS metadata")
        anchor = str(metadata.get("anchor_state_sha256") or "")
        expected = Path("objects") / "sha256" / digest[:2] / \
            f"{digest}.json.gz"
        if digest in official_objects or anchor not in official_objects or \
                anchor == digest or metadata.get("path") != \
                expected.as_posix() or \
                metadata.get("state_sha256") != digest or \
                metadata.get("origin") != \
                "derived_verified_reverse_replay" or \
                metadata.get("source_exact") is not False or \
                not _DIGEST.fullmatch(str(metadata.get("gzip_sha256") or "")) \
                or isinstance(metadata.get("gzip_bytes"), bool) or \
                not isinstance(metadata.get("gzip_bytes"), int) or \
                metadata["gzip_bytes"] <= 0 or \
                isinstance(metadata.get("canonical_bytes"), bool) or \
                not isinstance(metadata.get("canonical_bytes"), int) or \
                metadata["canonical_bytes"] <= 0:
            raise RuntimeError(f"unsafe derived CAS metadata: {digest}")
        source = store / expected
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"missing or unsafe derived object: {digest}")
        compressed_hash, compressed_size = file_sha256(source)
        if compressed_hash != metadata["gzip_sha256"] or \
                compressed_size != metadata["gzip_bytes"]:
            raise RuntimeError(f"derived gzip metadata mismatch: {digest}")
        state = load_official_state(store, digest)
        if len(canonical_json_bytes(state)) != metadata["canonical_bytes"]:
            raise RuntimeError(f"derived canonical size mismatch: {digest}")
        anchor_state = load_official_state(store, anchor)
        projection = ("id", "jurabk", "juris", "title", "stand", "build")
        if any(state[field] != anchor_state[field] for field in projection):
            raise RuntimeError(f"derived anchor projection drift: {digest}")
        states_by_digest[digest] = state
        expected_paths.add(expected)

    seen_ids: set[str] = set()
    row_digests: set[str] = set()
    for row in rows:
        row_id = row.get("id") if isinstance(row, dict) else None
        digest = str(row.get("state_sha256") or "") \
            if isinstance(row, dict) else ""
        if not isinstance(row_id, str) or not row_id or row_id in seen_ids or \
                digest not in states_by_digest:
            raise RuntimeError("invalid or duplicate verified reconstruction")
        seen_ids.add(row_id)
        row_digests.add(digest)
        state = states_by_digest[digest]
        anchor = metadata_by_digest[digest]["anchor_state_sha256"]
        if row.get("anchor_state_sha256") != anchor or \
                row.get("act_id") != state["id"] or \
                row.get("jurabk") != state["jurabk"] or \
                row.get("text_status") != "derived_verified" or \
                row.get("body_complete") is not True or \
                row.get("source_exact") is not False or \
                row.get("reverse_replay_verified") is not True or \
                row.get("anchor_projection_metadata_retained") is not True:
            raise RuntimeError(
                f"verified reconstruction crossed evidence boundary: {row_id}")
    if row_digests != set(metadata_by_digest):
        raise RuntimeError("verified reconstruction object set mismatch")

    target_store = out / "federal_states"
    _sync_exported_objects(target_store, expected_paths)
    for digest, metadata in sorted(metadata_by_digest.items()):
        relative = Path(metadata["path"])
        copy_verified_file(
            store / relative, target_store / relative,
            metadata["gzip_sha256"], metadata["gzip_bytes"])
    # Publish the manifest only after every referenced byte object is present.
    write_bytes_atomic(out / "verified_reconstructions.json", artifact_bytes)
    return len(rows), len(metadata_by_digest)


def _official_neuris_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "testphase.rechtsinformationen.bund.de"
        and parsed.username is None and parsed.password is None
        and port in (None, 443)
        and parsed.path.startswith("/v1/legislation/")
    )


def _valid_neuris_payload(path: Path, suffix: str) -> bool:
    if suffix == ".zip":
        return zipfile.is_zipfile(path)
    if suffix == ".xml":
        try:
            ET.parse(path)
        except (ET.ParseError, OSError):
            return False
        return True
    if suffix == ".html":
        try:
            prefix = path.read_bytes()[:4096].lstrip().lower()
        except OSError:
            return False
        return bool(prefix) and (
            b"<html" in prefix or prefix.startswith(b"<!doctype html"))
    return False


def _export_neuris_archive(
        out: Path, archive_path: Path = NEURIS_ARCHIVE,
        objects: Path = NEURIS_OBJECTS) -> tuple[int, int]:
    """Export the exact NeuRIS ledger and each hash-verified captured object."""
    archive_path = require_file(Path(archive_path), "NeuRIS archive ledger")
    rows: list[dict] = []
    try:
        archive_bytes = archive_path.read_bytes()
        for number, line in enumerate(
                archive_bytes.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"NeuRIS archive row {number} is not an object")
            rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("NeuRIS archive is invalid JSONL") from exc
    if not rows:
        raise RuntimeError("NeuRIS archive ledger is empty")

    ids: set[str] = set()
    captured: dict[Path, tuple[str, int, str]] = {}
    for row in rows:
        event_id = row.get("event_id")
        status = row.get("capture_status")
        content_url = row.get("content_url")
        if not isinstance(event_id, str) or not event_id or event_id in ids or \
                row.get("source") != "neuris_changelog" or \
                row.get("kind") not in {
                    "consolidation_changed", "consolidation_deleted"} or \
                row.get("legal_effect") != "not_asserted" or \
                row.get("date_basis") != \
                "retrieval_observation_and_eli_identifiers_not_legal_effect" or \
                not _official_neuris_url(content_url) or \
                not isinstance(status, str) or not status:
            raise RuntimeError(f"unsafe NeuRIS archive row: {event_id!r}")
        ids.add(event_id)
        if status != "captured":
            if row.get("content_sha256") is not None or \
                    row.get("content_bytes") is not None or \
                    row.get("content_object") is not None:
                raise RuntimeError(
                    f"uncaptured NeuRIS row references bytes: {event_id}")
            continue

        digest = str(row.get("content_sha256") or "")
        size = row.get("content_bytes")
        object_name = row.get("content_object")
        match = _NEURIS_OBJECT.fullmatch(str(object_name or ""))
        if not match or match.group("sha") != digest or \
                not _DIGEST.fullmatch(digest) or isinstance(size, bool) or \
                not isinstance(size, int) or size <= 0 or \
                row.get("content_media_type") != \
                _NEURIS_MEDIA[match.group("suffix")] or \
                row.get("content_source_url") != content_url:
            raise RuntimeError(
                f"captured NeuRIS metadata is inconsistent: {event_id}")
        relative = Path(object_name)
        source = Path(objects) / relative.name
        if source.is_symlink() or not source.is_file() or \
                file_sha256(source) != (digest, size) or \
                not _valid_neuris_payload(source, match.group("suffix")):
            raise RuntimeError(
                f"captured NeuRIS object failed verification: {event_id}")
        prior = captured.get(relative)
        details = (digest, size, match.group("suffix"))
        if prior is not None and prior != details:
            raise RuntimeError(f"NeuRIS CAS path collision: {relative}")
        captured[relative] = details

    target_objects = out / "neuris_objects"
    expected = {Path(path.name) for path in captured}
    _sync_exported_objects(target_objects, expected)
    for relative, (digest, size, _suffix) in sorted(
            captured.items(), key=lambda item: item[0].as_posix()):
        copy_verified_file(
            Path(objects) / relative.name, out / relative, digest, size)
    # Preserve the logical append-only ledger byte-for-byte, including failed
    # and metadata-only observations; none of them can reference partial bytes.
    write_bytes_atomic(out / "neuris_archive.jsonl", archive_bytes)
    return len(rows), len(captured)


CARD = """---
license: other
language:
  - de
tags:
  - legal
  - germany
  - legislation
  - eu-law
  - court-decisions
  - temporal-data
pretty_name: Lexgraph — Laws as Git for German and EU law
configs:
{configs}
---

# Lexgraph dataset

The data plane of **[Lexgraph](https://github.com/SNTIQ-Team/lexgraph)** —
German legislation modelled as **Laws as Git**: a temporal, multi-authority
event log with HEAD, commits, open/closed branches and evidence-bound merges
(Bund / Bayern / EU; Länder records only after verification at the originating
Landtag). Built **{today}**.

Git is a navigation metaphor, not a substitute for legal status. Every row's
official source and status controls whether it is current law, a pending branch
or a documented implementation/applicability link.

Lexgraph is deliberately two-layered. The German migration, asylum, social-law
and related practice corpus has current full text and change history where the
official/retrieval sources support it. `federal_catalog.jsonl` provides every
act listed in the official GII table of contents as discovery metadata only;
`eu_index.jsonl` provides EU breadth: all
in-force directives (including implementing/delegated directives) and basic
regulations exposed by CELLAR, as metadata only. It is not presented as deep
coverage of every EU instrument.

The independent federal archive is split by claim strength. An
`official_federal_state_observations.jsonl` row says only what complete GII
state Lexgraph retrieved on `observed_at`; that date is **not** silently
treated as commencement. `official_federal_state_transitions.jsonl` contains
Lexgraph's own old/new diff between two such states and likewise leaves
`effective_at` empty. A row enters `official_transition_reviews.jsonl` only
after the complete state pair is matched to the final, integrity-checked BGBl
amending command and the exact DIP commencement clause. Full portable states,
including every captured norm, are in
`official_federal_state_objects.jsonl` and are identified by the SHA-256 of
canonical uncompressed JSON.

`retrospective_legal_intervals.jsonl` adds a true bitemporal projection:
legal validity (`effective_from`/`effective_to`) is independent from Lexgraph
knowledge time (`knowledge_from`/`knowledge_to`). The 2023+ final-BGBl
inventory is exported separately as `retrospective_amendment_events.jsonl`.
An event with a publication/effective date is not thereby a reconstructed
historical consolidated text; unresolved sub-article dates and missing state
pairs remain explicit in `retrospective_gaps.jsonl`. The complete manifest and
a portable indexed SQLite representation are included as artifacts.

`verified_reconstructions.json` is a separate, reviewed claim class. Its
`derived_verified` bodies were computed by reversing cardinality-checked final
BGBl commands from a complete official GII anchor and then replaying the
commands forward to reproduce that anchor byte-for-byte. They are complete
Lexgraph reconstructions. The artifact deliberately keeps `source_exact: false`
because NeuRIS/GII did not supply those bytes as a historical snapshot.
The referenced deterministic-gzip objects are retained under
`federal_states/objects/` and remain separate from the official GII manifest.

Buzer is not an input to these four files and no private Buzer snapshot or
synopsis is shipped. It may be used outside the export as a non-authoritative
human QA/deep-link cross-check; all published state bytes, diffs and legal-date
evidence above are reproduced independently from official sources.

| File | Rows | Content |
|---|---:|---|
{rows_table}

`decisions.jsonl` combines reviewed cases with a forward-cumulative,
corpus-filtered import from the seven official federal
Rechtsprechung-im-Internet feeds. Automated norm links are citations, not
claims that the court interpreted every cited provision. The table is not a
catalogue of all German case law.

`neuris_archive.jsonl` is the exact logical NeuRIS changelog ledger. It keeps
metadata-only, tombstone and failed-capture observations so the archive does
not hide gaps. Only rows with `capture_status: captured` may reference bytes;
each referenced official ZIP/XML/HTML artifact is re-hashed and exported under
`neuris_objects/`. Temporary downloads, failed partials and unreferenced local
cache files are excluded. ELI point-in-time and manifestation components are
source identifiers, not silently asserted legal-effective dates.

For Bavaria, official version rows are amendment metadata. Word-level history
is exported only when archived official pages yield an unambiguous state
transition, plus complete forward diffs between daily snapshots. Missing
history is kept missing instead of reconstructed. In
`bayern_word_diffs.jsonl`, `date` is the promulgation/event date while
`effective_date` is the date on which the consolidated wording changes; data
consumers doing time travel must prefer `effective_date`.

The public API can serialize the current complete act or one provision as
Markdown. It can also check out an exact archived GII observation when the
requested date exists in the state store. Historical output remains explicitly
labelled partial whenever the available source only supplies amendment
excerpts rather than a lossless consolidated snapshot; the dataset does not
imply exact historical text where the source archive cannot prove it.

`watched_procedures.json` preserves the current DIP/EUR-Lex observations,
official document chronology and embedded status-change history. Its
`analysis` object keeps verified facts, deterministic inferences and a
qualitative forecast separate; likelihood is not presented as a statistically
calibrated probability. Terminal procedures remain archived but leave the
active polling set. `amendment_fates.json` separates reviewed roles in a
parliamentary document chain from the mechanical checks performed against the
current consolidated corpus.

DIP-derived rows use the attribution **Deutscher Bundestag/Bundesrat – DIP**.
Lexgraph extraction, ranking, annotations and forecasts are transformations,
not source statements. DIP makes its source data available free of charge at
[dip.bundestag.de](https://dip.bundestag.de).

Every source and file-level rights regime is documented in the repository's
`docs/SOURCES.md` and `docs/RIGHTS.md`; reproduce
the current outputs with `refresh.sh` and `tools/export_hf.py`. Statutory texts
are official works under § 5 UrhG. The dataset is intentionally marked
`license: other`: SNTIQ's licence covers its original annotations and software,
not third-party or official source material. See `RIGHTS.md` before reuse.

Built by **[SNTIQ](https://sntiq.com/)**.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "hf_export"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    policy_path = require_file(
        ROOT / "web" / "data" / "data_policy.json", "public data policy")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("includes_quarantined_sources") or not policy.get(
            "public_build"):
        raise RuntimeError(
            "refusing HF export: rebuild web data without quarantined sources")

    # Avoid stale files surviving when the exported schema changes.
    for path in out.iterdir():
        if path.is_file():
            path.unlink()

    descriptions: dict[str, str] = {}
    counts: dict[str, int] = {}
    for name, (source, filename, description) in SNAPSHOT_FILES.items():
        snap = latest_snapshot(source)
        src = require_file(
            snap / filename if snap else Path(""), f"{source}/{filename}")
        dst = out / name
        copy_file(src, dst)
        counts[name] = line_count(dst)
        descriptions[name] = description

    for name, (src, description) in DERIVED_FILES.items():
        dst = out / name
        copy_file(require_file(src, name), dst)
        counts[name] = line_count(dst) if dst.suffix == ".jsonl" else 1
        descriptions[name] = description

    reconstruction_count, reconstruction_object_count = \
        _export_verified_reconstructions(out)
    counts["verified_reconstructions.json"] = reconstruction_count
    descriptions["verified_reconstructions.json"] = (
        "reviewed complete reverse-replay reconstructions, explicitly "
        "source_exact=false")
    counts["federal_states/objects/"] = reconstruction_object_count
    descriptions["federal_states/objects/"] = (
        "deterministic-gzip CAS bytes referenced by verified reconstructions")

    neuris_event_count, neuris_object_count = _export_neuris_archive(out)
    counts["neuris_archive.jsonl"] = neuris_event_count
    descriptions["neuris_archive.jsonl"] = (
        "resumable official NeuRIS changelog ledger with honest capture status")
    counts["neuris_objects/"] = neuris_object_count
    descriptions["neuris_objects/"] = (
        "hash-verified official NeuRIS source objects referenced by the ledger")

    official_rows = _official_history_rows()
    for name, description in OFFICIAL_HISTORY_FILES.items():
        counts[name] = write_jsonl(out / name, official_rows[name])
        descriptions[name] = description

    retrospective_rows = _retrospective_rows()
    for name, description in RETROSPECTIVE_FILES.items():
        counts[name] = write_jsonl(out / name, retrospective_rows[name])
        descriptions[name] = description

    optional_exported: list[str] = []
    for name, (src, description) in OPTIONAL_DERIVED_FILES.items():
        if not src.is_file() or src.stat().st_size == 0:
            continue
        dst = out / name
        copy_file(src, dst)
        counts[name] = line_count(dst) if dst.suffix == ".jsonl" else 1
        descriptions[name] = description
        optional_exported.append(name)

    counts["decisions.jsonl"] = write_jsonl(
        out / "decisions.jsonl", merged_decisions())
    descriptions["decisions.jsonl"] = (
        "reviewed and official federal decisions affecting the deep corpus")

    ordered = (list(SNAPSHOT_FILES) + list(OFFICIAL_HISTORY_FILES)
               + list(RETROSPECTIVE_FILES)
               + list(DERIVED_FILES)
               + ["verified_reconstructions.json",
                  "federal_states/objects/", "neuris_archive.jsonl",
                  "neuris_objects/"]
               + optional_exported + ["decisions.jsonl"])
    rows_table = "\n".join(
        f"| `{name}` | {counts[name]:,} | {descriptions[name]} |"
        for name in ordered)
    configs = "\n".join(
        f"  - config_name: {Path(name).stem}\n    data_files: {name}"
        for name in ordered if name.endswith(".jsonl"))
    (out / "README.md").write_text(CARD.format(
        today=date.today().isoformat(), configs=configs,
        rows_table=rows_table), encoding="utf-8")
    copy_file(require_file(ROOT / "docs" / "RIGHTS.md", "rights matrix"),
              out / "RIGHTS.md")

    for name in ordered:
        print(f"  {name}: {counts[name]:,}")
    print(f"dataset card -> {out / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
