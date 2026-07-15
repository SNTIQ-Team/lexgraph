"""Capture the NeuRIS legislation changelog and its ephemeral ZIP objects.

NeuRIS (testphase.rechtsinformationen.bund.de) exposes an official changelog
for consolidated federal legislation.  A changelog ``contentUrl`` is not an
archive: old expressions can disappear.  Consequently every selected
``changed`` ZIP is downloaded *before* the event is committed to the
cumulative ledger and stored in a content-addressed local object store.

Default capture is deliberately bounded.  Only work ELIs mapped to the latest
curated GII corpus are eligible.  The persisted mapping is resolved through
the official ``GET /v1/legislation?abbreviation=...`` endpoint and accepts
only an exact, unambiguous abbreviation result.  ``--capture-all`` is an
explicit opt-in, and both modes retain download-count and byte guards.

Generated local state (ignored by Git):

``data/neuris_archive.jsonl``
    Logical append-only event ledger.  Existing events may only gain capture
    metadata; events and tombstones are never removed.
``data/neuris_work_map.json``
    Persisted bidirectional curated GII slug/jurabk <-> NeuRIS work-ELI map.
``data/neuris_objects/<sha256>.zip``
    Deterministic, content-addressed, hash-verified ZIP object store.

The ELI point-in-time and manifestation components are source identifiers.
Neither they nor the retrieval observation are asserted here to be the legal
effective date of a provision.

Endpoint (audited 2026-07-15; only /v1 is robots-sanctioned):
    GET /v1/legislation/changelog?from=<iso>&to=<iso>
    -> {"changed":[{contentUrl}...], "deleted":[...], "allChanged":bool}

ELI anatomy in contentUrl:
    /v1/legislation/eli/bund/bgbl-1/1957/s652/2025-01-01/1/deu/2024-01-23.zip
        work ELI ----------^^^^^^^^  source PIT^ ver lang  manifestation^
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from common import ROOT, Http, latest_snapshot, read_jsonl, snapshot_dir

BASE = "https://testphase.rechtsinformationen.bund.de"
ARCHIVE = ROOT / "data" / "neuris_archive.jsonl"
WORK_MAP = ROOT / "data" / "neuris_work_map.json"
OBJECTS = ROOT / "data" / "neuris_objects"

DEFAULT_MAX_DOWNLOADS = 64
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
UNRESOLVED_RETRY_DAYS = 7
CHUNK_SIZE = 64 * 1024

ELI_RE = re.compile(
    r"(?P<work>eli/bund/[^/]+/\d{4}/[^/]+)"
    r"/(?P<pit>\d{4}-\d{2}-\d{2})/(?P<ver>\d+)/(?P<lang>[a-z]{3})"
    r"(?:/(?P<manifested>\d{4}-\d{2}-\d{2}))?")
WORK_ELI_RE = re.compile(r"^eli/bund/[^/]+/\d{4}/[^/]+$")

# GII retains historical JurAbk/year suffixes for a handful of consolidated
# acts.  These aliases are intentionally explicit; there is no fuzzy title
# matching.  SGB aliases are generated separately with a bounded Roman table.
ALIASES_BY_SLUG: dict[str, tuple[str, ...]] = {
    "asylvfg_1992": ("AsylG",),
    "aufenthg_2004": ("AufenthG",),
    "beschv_2013": ("BeschV",),
    "bkgg_1996": ("BKGG",),
    "freiz_gg_eu_2004": ("FreizügG/EU",),
    "rbeg_2021": ("RBEG",),
    "sgb_9_2018": ("SGB IX",),
    "stag": ("StAG",),
    "waffg_2002": ("WaffG",),
}
ROMAN = {
    1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII",
    8: "VIII", 9: "IX", 10: "X", 11: "XI", 12: "XII", 13: "XIII",
    14: "XIV",
}

CAPTURE_FIELDS = {
    "capture_status", "capture_scope", "capture_reused", "captured_at",
    "content_sha256", "content_bytes", "content_object", "mapped_jurabk",
    "mapped_slug", "capture_error",
}


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write bytes in the target directory, fsync, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    payload = b"".join(
        (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        .encode("utf-8") for row in rows)
    _atomic_write(path, payload)


def atomic_write_json(path: Path, value: dict) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_write(path, payload)


def _record_url(record: object) -> str:
    if isinstance(record, str):
        return record
    if isinstance(record, dict):
        for key in ("contentUrl", "@id", "legislationIdentifier"):
            value = record.get(key)
            if isinstance(value, str):
                return value
    return ""


def to_event(url: str, kind: str, fetched_at: str) -> dict:
    m = ELI_RE.search(url)
    d = m.groupdict() if m else {}
    absolute = url if url.startswith("http") else BASE + url
    return {
        "event_id": f"event:neuris:{kind}:{url.rsplit('/v1/', 1)[-1]}",
        "kind": kind,
        "actor": "NeuRIS (BMJ/DigitalService)",
        # LexEvent ordering is the observation time.  The ELI date components
        # remain separately named source identifiers and are not legal dates.
        "time": fetched_at,
        "observed_at": fetched_at,
        "eli_work": d.get("work"),
        "point_in_time": d.get("pit"),
        "eli_point_in_time": d.get("pit"),
        "expression_version": d.get("ver"),
        "eli_manifestation_date": d.get("manifested"),
        "content_url": absolute,
        "legal_effect": "not_asserted",
        "date_basis": "retrieval_observation_and_eli_identifiers_not_legal_effect",
        "source": "neuris_changelog",
        "fetched_at": fetched_at,
    }


def events_from_changelog(data: dict, fetched_at: str) -> list[dict]:
    events = [
        to_event(_record_url(row), "consolidation_changed", fetched_at)
        for row in data.get("changed", [])
    ]
    events.extend(
        to_event(_record_url(row), "consolidation_deleted", fetched_at)
        for row in data.get("deleted", [])
    )
    return [event for event in events if event["eli_work"]]


def _roman_sgb_alias(jurabk: str) -> str | None:
    match = re.fullmatch(r"SGB\s+(\d+)(?:\s+\d{4})?", jurabk.strip(), re.I)
    if not match:
        return None
    roman = ROMAN.get(int(match.group(1)))
    return f"SGB {roman}" if roman else None


def abbreviation_candidates(act: dict) -> tuple[str, ...]:
    """Return conservative exact abbreviations to ask NeuRIS for."""
    slug = str(act.get("slug") or "").strip().lower()
    jurabk = " ".join(str(act.get("jurabk") or "").split())
    candidates: list[str] = list(ALIASES_BY_SLUG.get(slug, ()))
    roman = _roman_sgb_alias(jurabk)
    if roman:
        candidates.insert(0, roman)
    if jurabk:
        candidates.append(jurabk)
    # A trailing source-edition year is removed only as a second exact query;
    # the result still has to echo that exact abbreviation unambiguously.
    stripped = re.sub(r"\s+(?:19|20)\d{2}$", "", jurabk)
    if stripped and stripped != jurabk:
        candidates.append(stripped)
    return tuple(dict.fromkeys(candidates))


def _exact_work_from_search(data: object, abbreviation: str) -> str | None:
    if not isinstance(data, dict):
        return None
    works: set[str] = set()
    for wrapper in data.get("member", []):
        item = wrapper.get("item", {}) if isinstance(wrapper, dict) else {}
        if not isinstance(item, dict):
            continue
        returned = " ".join(str(item.get("abbreviation") or "").split())
        if returned.casefold() != abbreviation.casefold():
            continue
        work = item.get("exampleOfWork", {})
        work = work.get("legislationIdentifier") if isinstance(work, dict) \
            else None
        if isinstance(work, str) and WORK_ELI_RE.fullmatch(work):
            works.add(work)
    return next(iter(works)) if len(works) == 1 else None


def _blank_work_map() -> dict:
    return {"schema_version": 1, "updated_at": None, "by_slug": {},
            "by_work": {}, "unresolved": {}}


def load_work_map(path: Path = WORK_MAP) -> dict:
    if not path.exists():
        return _blank_work_map()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank_work_map()
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return _blank_work_map()
    for key in ("by_slug", "by_work", "unresolved"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _retry_unresolved(row: object, now: datetime) -> bool:
    if not isinstance(row, dict) or not row.get("checked_at"):
        return True
    try:
        checked = datetime.fromisoformat(str(row["checked_at"]))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return now - checked >= timedelta(days=UNRESOLVED_RETRY_DAYS)


def resolve_curated_work_map(http: Http, acts: list[dict], *, now: datetime,
                             path: Path = WORK_MAP,
                             refresh: bool = False) -> dict:
    """Resolve and persist exact official work mappings for current GII acts."""
    state = load_work_map(path)
    old_by_slug = state["by_slug"]
    old_unresolved = state["unresolved"]
    by_slug: dict[str, dict] = {}
    unresolved: dict[str, dict] = {}
    checked_at = now.isoformat(timespec="seconds")

    for act in sorted(acts, key=lambda row: str(row.get("slug") or "")):
        slug = str(act.get("slug") or "").strip().lower()
        jurabk = " ".join(str(act.get("jurabk") or "").split())
        if not slug or not jurabk:
            continue
        previous = old_by_slug.get(slug)
        if (not refresh and isinstance(previous, dict)
                and previous.get("jurabk") == jurabk
                and WORK_ELI_RE.fullmatch(str(previous.get("eli_work") or ""))):
            by_slug[slug] = previous
            continue
        previous_miss = old_unresolved.get(slug)
        if (not refresh and isinstance(previous_miss, dict)
                and previous_miss.get("jurabk") == jurabk
                and not _retry_unresolved(previous_miss, now)):
            unresolved[slug] = previous_miss
            continue

        queries = abbreviation_candidates(act)
        resolved: dict | None = None
        for abbreviation in queries:
            response = http.get(
                f"{BASE}/v1/legislation",
                params={"abbreviation": abbreviation, "size": 100},
                timeout=60,
            )
            if response.status_code != 200:
                continue
            try:
                search_result = response.json()
            except ValueError:
                continue
            work = _exact_work_from_search(search_result, abbreviation)
            if work:
                resolved = {
                    "slug": slug, "jurabk": jurabk,
                    "query_abbreviation": abbreviation,
                    "eli_work": work, "resolved_at": checked_at,
                }
                break
        if resolved:
            by_slug[slug] = resolved
        else:
            unresolved[slug] = {
                "slug": slug, "jurabk": jurabk,
                "queries": list(queries), "checked_at": checked_at,
                "reason": "no_exact_unambiguous_official_result",
            }

    by_work: dict[str, dict] = {}
    for entry in by_slug.values():
        work = entry["eli_work"]
        # Two curated acts resolving to the same work is a mapping ambiguity;
        # exclude it from capture instead of guessing.
        if work in by_work:
            by_work[work] = {"ambiguous": True}
        else:
            by_work[work] = {
                "slug": entry["slug"], "jurabk": entry["jurabk"],
                "query_abbreviation": entry["query_abbreviation"],
            }
    ambiguous = {work for work, row in by_work.items() if row.get("ambiguous")}
    if ambiguous:
        by_slug = {slug: row for slug, row in by_slug.items()
                   if row["eli_work"] not in ambiguous}
        by_work = {work: row for work, row in by_work.items()
                   if work not in ambiguous}

    state = {
        "schema_version": 1, "updated_at": checked_at,
        "by_slug": by_slug, "by_work": by_work,
        "unresolved": unresolved,
    }
    atomic_write_json(path, state)
    return state


def load_curated_gii_acts() -> list[dict]:
    latest = latest_snapshot("gii")
    acts_path = latest / "acts.jsonl" if latest else None
    return list(read_jsonl(acts_path)) if acts_path and acts_path.exists() else []


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _valid_existing_object(objects: Path, sha256: str,
                           expected_size: int | None) -> bool:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return False
    path = objects / f"{sha256}.zip"
    if not path.is_file():
        return False
    actual_hash, actual_size = _sha256_file(path)
    if actual_hash != sha256:
        raise RuntimeError(f"CAS hash mismatch: {path}")
    return expected_size is None or actual_size == expected_size


@dataclass
class CaptureBudget:
    max_downloads: int
    max_bytes: int
    downloads: int = 0
    bytes_downloaded: int = 0

    @property
    def bytes_left(self) -> int:
        return max(0, self.max_bytes - self.bytes_downloaded)


def _capture_url(http: Http, url: str, objects: Path,
                 budget: CaptureBudget) -> dict:
    if budget.downloads >= budget.max_downloads:
        return {"capture_status": "limit_downloads"}
    if budget.bytes_left <= 0:
        return {"capture_status": "limit_bytes"}

    budget.downloads += 1
    try:
        response = http.get(url, timeout=180, stream=True)
    except Exception as exc:  # Http already exhausted its bounded retries
        return {"capture_status": "download_error",
                "capture_error": type(exc).__name__}
    if response.status_code != 200:
        close = getattr(response, "close", None)
        if close:
            close()
        return {"capture_status": f"http_{response.status_code}"}

    content_length = response.headers.get("Content-Length") \
        if hasattr(response, "headers") else None
    try:
        declared_size = int(content_length) if content_length else None
    except (TypeError, ValueError):
        declared_size = None
    if declared_size is not None and declared_size > budget.bytes_left:
        close = getattr(response, "close", None)
        if close:
            close()
        return {"capture_status": "limit_bytes"}

    objects.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".capture-", suffix=".part",
                                    dir=objects)
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            iterator = response.iter_content(chunk_size=CHUNK_SIZE)
            for chunk in iterator:
                if not chunk:
                    continue
                if len(chunk) > budget.bytes_left:
                    # The response has already yielded this bounded chunk, but
                    # no over-budget byte is written to the object store.
                    budget.bytes_downloaded += len(chunk)
                    return {"capture_status": "limit_bytes"}
                size += len(chunk)
                budget.bytes_downloaded += len(chunk)
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if not zipfile.is_zipfile(tmp):
            return {"capture_status": "invalid_zip"}
        sha256 = digest.hexdigest()
        target = objects / f"{sha256}.zip"
        if target.exists():
            actual_hash, actual_size = _sha256_file(target)
            if actual_hash != sha256 or actual_size != size:
                raise RuntimeError(f"CAS hash mismatch: {target}")
            reused = True
        else:
            os.replace(tmp, target)
            actual_hash, actual_size = _sha256_file(target)
            if actual_hash != sha256 or actual_size != size:
                raise RuntimeError(f"CAS verification failed: {target}")
            reused = False
        return {
            "capture_status": "captured",
            "capture_reused": reused,
            "content_sha256": sha256,
            "content_bytes": size,
            "content_object": f"neuris_objects/{sha256}.zip",
        }
    except RuntimeError:
        raise
    except Exception as exc:
        return {"capture_status": "download_error",
                "capture_error": type(exc).__name__}
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
        tmp.unlink(missing_ok=True)


def _read_archive(path: Path) -> list[dict]:
    rows = list(read_jsonl(path)) if path.exists() else []
    return [_normalize_legacy_event(row) for row in rows]


def _normalize_legacy_event(source: dict) -> dict:
    """Migrate old metadata honestly without deleting its event identity."""
    row = dict(source)
    if row.get("source") != "neuris_changelog":
        return row
    fetched_at = row.get("fetched_at")
    old_time = row.get("time")
    if fetched_at:
        if old_time and old_time != fetched_at:
            row.setdefault("legacy_source_time", old_time)
        row["time"] = fetched_at
        row.setdefault("observed_at", fetched_at)
    row.setdefault("eli_point_in_time", row.get("point_in_time"))
    if "eli_manifestation_date" not in row:
        match = ELI_RE.search(str(row.get("content_url") or ""))
        row["eli_manifestation_date"] = match.group("manifested") \
            if match else None
    row["legal_effect"] = "not_asserted"
    row["date_basis"] = \
        "retrieval_observation_and_eli_identifiers_not_legal_effect"
    if "capture_status" not in row:
        row["capture_status"] = (
            "tombstone" if row.get("kind") == "consolidation_deleted"
            else "legacy_metadata_only_not_captured")
    row.setdefault("capture_scope", "metadata_only")
    row.setdefault("content_sha256", None)
    row.setdefault("content_bytes", None)
    return row


def capture_events(events: list[dict], http: Http, work_map: dict, *,
                   archive_path: Path = ARCHIVE,
                   objects: Path = OBJECTS,
                   capture_all: bool = False,
                   max_downloads: int = DEFAULT_MAX_DOWNLOADS,
                   max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[list[dict], CaptureBudget]:
    """Capture eligible changed events before any archive mutation."""
    prior = {row.get("event_id"): row for row in _read_archive(archive_path)}
    by_work = work_map.get("by_work", {}) if isinstance(work_map, dict) else {}
    budget = CaptureBudget(max_downloads=max_downloads, max_bytes=max_bytes)
    enriched: list[dict] = []

    for source_event in events:
        event = dict(source_event)
        mapping = by_work.get(event.get("eli_work"))
        if isinstance(mapping, dict) and not mapping.get("ambiguous"):
            event["mapped_jurabk"] = mapping.get("jurabk")
            event["mapped_slug"] = mapping.get("slug")
        if event["kind"] == "consolidation_deleted":
            event.update({
                "capture_status": "tombstone",
                "capture_scope": "metadata_only",
                "content_sha256": None,
                "content_bytes": None,
            })
            enriched.append(event)
            continue
        if not capture_all and not mapping:
            event.update({
                "capture_status": "metadata_only_unmapped",
                "capture_scope": "curated_gii",
                "content_sha256": None,
                "content_bytes": None,
            })
            enriched.append(event)
            continue

        event["capture_scope"] = "all_opt_in" if capture_all else "curated_gii"
        old = prior.get(event["event_id"], {})
        old_sha = old.get("content_sha256") if isinstance(old, dict) else None
        old_size = old.get("content_bytes") if isinstance(old, dict) else None
        if (isinstance(old_sha, str)
                and _valid_existing_object(objects, old_sha,
                                           old_size if isinstance(old_size, int)
                                           else None)):
            event.update({
                "capture_status": "captured", "capture_reused": True,
                "captured_at": old.get("captured_at") or old.get("fetched_at"),
                "content_sha256": old_sha, "content_bytes": old_size,
                "content_object": f"neuris_objects/{old_sha}.zip",
            })
        else:
            result = _capture_url(http, event["content_url"], objects, budget)
            event.update(result)
            event.setdefault("content_sha256", None)
            event.setdefault("content_bytes", None)
            if result["capture_status"] == "captured":
                event["captured_at"] = event["fetched_at"]
        enriched.append(event)
    return enriched, budget


def update_archive(path: Path, events: list[dict]) -> tuple[int, int, int]:
    """Logically append events; only upgrade capture metadata in old rows."""
    rows = _read_archive(path)
    positions = {row.get("event_id"): i for i, row in enumerate(rows)}
    added = enriched = 0
    for event in events:
        event_id = event["event_id"]
        if event_id not in positions:
            positions[event_id] = len(rows)
            rows.append(event)
            added += 1
            continue
        index = positions[event_id]
        old = rows[index]
        old_status = old.get("capture_status")
        new_status = event.get("capture_status")
        should_upgrade = (
            old_status is None
            or (old_status != "captured" and new_status == "captured")
        )
        if should_upgrade:
            upgraded = dict(old)
            for key in CAPTURE_FIELDS:
                if key in event:
                    upgraded[key] = event[key]
            rows[index] = upgraded
            enriched += 1
    atomic_write_jsonl(path, rows)
    return added, enriched, len(rows)


def _positive_or_zero(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Archive NeuRIS changelog metadata and ephemeral ZIPs.")
    ap.add_argument("--days", type=int, default=None,
                    help="window length; default: since previous snapshot")
    ap.add_argument(
        "--capture-all", action="store_true",
        help=("capture every changed ZIP, including works outside the curated "
              "GII corpus; explicit opt-in, safety caps still apply"))
    ap.add_argument(
        "--max-downloads", type=_positive_or_zero,
        default=DEFAULT_MAX_DOWNLOADS,
        help=f"maximum ZIP requests this run (default: {DEFAULT_MAX_DOWNLOADS})")
    ap.add_argument(
        "--max-bytes", type=_positive_or_zero, default=DEFAULT_MAX_BYTES,
        help=("maximum streamed ZIP bytes this run "
              f"(default: {DEFAULT_MAX_BYTES})"))
    ap.add_argument(
        "--refresh-work-map", action="store_true",
        help="re-resolve every curated GII abbreviation through official NeuRIS")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    prev = latest_snapshot("neuris_changelog")
    if args.days:
        since = now - timedelta(days=args.days)
    elif prev:                      # overlap 1 day; archive dedupes
        y, m, dd = map(int, prev.name.split("-"))
        since = datetime(y, m, dd, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        since = now - timedelta(days=30)

    http = Http(delay=0.5)
    url = (f"{BASE}/v1/legislation/changelog"
           f"?from={since:%Y-%m-%dT%H:00:00Z}&to={now:%Y-%m-%dT%H:00:00Z}")
    response = http.get(url, timeout=120)
    if response.status_code != 200:
        print(f"changelog HTTP {response.status_code}", file=sys.stderr)
        return 1
    data = response.json()
    fetched_at = now.isoformat(timespec="seconds")
    events = events_from_changelog(data, fetched_at)

    acts = load_curated_gii_acts()
    work_map = resolve_curated_work_map(
        http, acts, now=now, path=WORK_MAP, refresh=args.refresh_work_map)
    events, budget = capture_events(
        events, http, work_map, archive_path=ARCHIVE, objects=OBJECTS,
        capture_all=args.capture_all, max_downloads=args.max_downloads,
        max_bytes=args.max_bytes)

    # Capture is complete (or explicitly statused) before either durable event
    # file is touched.  Both writes use same-directory atomic replacement.
    out = snapshot_dir("neuris_changelog")
    atomic_write_jsonl(out / "events.jsonl", events)
    added, enriched, total = update_archive(ARCHIVE, events)

    works = {event["eli_work"] for event in events}
    captured = sum(event.get("capture_status") == "captured"
                   for event in events)
    metadata_only = sum(str(event.get("capture_status", "")).startswith(
        "metadata_only") for event in events)
    print(f"window {since:%Y-%m-%d} -> {now:%Y-%m-%d}: "
          f"{len(events)} events over {len(works)} works "
          f"(allChanged={data.get('allChanged')})")
    print(f"  capture   {captured} ZIPs represented, "
          f"{budget.downloads} requests / {budget.bytes_downloaded} bytes; "
          f"{metadata_only} metadata-only")
    print(f"  work map  {len(work_map['by_work'])} resolved / "
          f"{len(work_map['unresolved'])} unresolved curated acts")
    print(f"  snapshot -> {out}")
    print(f"  archive  +{added}, enriched {enriched} (total {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
