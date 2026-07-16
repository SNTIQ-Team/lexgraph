"""Validated bitemporal queries over Lexgraph retrospective history.

The manifest deliberately separates two clocks:

``effective_from`` / ``effective_to``
    Half-open legal-validity interval, expressed as German calendar dates.
``knowledge_from`` / ``knowledge_to``
    Half-open interval during which Lexgraph asserted that legal interval,
    expressed as RFC3339 instants.  A backfill performed today therefore does
    not pretend that Lexgraph already knew the reconstructed state years ago.

Complete state bodies stay in the existing content-addressed official GII
store.  The retrospective manifest contains only immutable SHA-256 identities
and evidence metadata, keeping the API process small on the production VPS.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from api.official_state_store import OfficialStateError, load_state_digest


SCHEMA_VERSION = 1
KIND = "lexgraph-retrospective-history"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_LEGAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TEXT_STATUSES = {"official_exact", "derived_verified", "partial"}
_DATE_STATUSES = {"official_verified", "derived", "unknown"}
_EVENT_TEXT_STATUSES = {"event_only"}


class RetrospectiveError(ValueError):
    """Base class for invalid or unresolvable retrospective data."""


class RetrospectiveIntegrityError(RetrospectiveError):
    """The manifest or one referenced immutable state failed validation."""


class RetrospectiveNotFound(RetrospectiveError):
    """No asserted state covers the requested legal and knowledge time."""


class RetrospectiveAmbiguity(RetrospectiveIntegrityError):
    """More than one state covers the same bitemporal coordinate."""


def _legal_date(value: Any, field: str, *, optional: bool = False
                ) -> str | None:
    if value is None and optional:
        return None
    raw = str(value or "")
    if not _LEGAL_DATE.fullmatch(raw):
        raise RetrospectiveIntegrityError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise RetrospectiveIntegrityError(f"{field} is not a real date") from exc


def _instant(value: Any, field: str, *, optional: bool = False
             ) -> datetime | None:
    if value is None and optional:
        return None
    raw = str(value or "").strip()
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrospectiveIntegrityError(
            f"{field} must be an RFC3339 instant") from exc
    if result.tzinfo is None:
        raise RetrospectiveIntegrityError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def _normalised_instant(value: Any, field: str) -> str:
    parsed = _instant(value, field)
    assert parsed is not None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _interval_contains(start: Any, end: Any, point: Any, *, dates: bool) -> bool:
    if dates:
        start_value = _legal_date(start, "interval start")
        end_value = _legal_date(end, "interval end", optional=True)
        point_value = _legal_date(point, "requested legal date")
    else:
        start_value = _instant(start, "knowledge_from")
        end_value = _instant(end, "knowledge_to", optional=True)
        point_value = _instant(point, "as_of")
    assert start_value is not None and point_value is not None
    return start_value <= point_value and (
        end_value is None or point_value < end_value)


def _validate_evidence(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise RetrospectiveIntegrityError("evidence must be a list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise RetrospectiveIntegrityError("evidence row must be an object")
        url = str(row.get("url") or "")
        if url and not url.startswith("https://"):
            raise RetrospectiveIntegrityError("evidence URL must use HTTPS")
        if str(row.get("source") or "").casefold() == "buzer":
            raise RetrospectiveIntegrityError(
                "third-party Buzer evidence is outside the official store")
        result.append(dict(row))
    return result


def _validate_interval(raw: Any, objects: dict[str, Any],
                       act_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RetrospectiveIntegrityError("history interval must be an object")
    row = dict(raw)
    assertion_id = str(row.get("id") or "")
    if not assertion_id:
        raise RetrospectiveIntegrityError("history interval has no assertion id")
    if row.get("act_id") not in (None, act_id):
        raise RetrospectiveIntegrityError("history interval act_id mismatch")
    effective_from = _legal_date(row.get("effective_from"), "effective_from")
    effective_to = _legal_date(
        row.get("effective_to"), "effective_to", optional=True)
    if effective_to is not None and effective_to <= effective_from:
        raise RetrospectiveIntegrityError(
            "effective_to must be after effective_from")
    knowledge_from = _instant(row.get("knowledge_from"), "knowledge_from")
    knowledge_to = _instant(
        row.get("knowledge_to"), "knowledge_to", optional=True)
    if knowledge_to is not None and knowledge_to <= knowledge_from:
        raise RetrospectiveIntegrityError(
            "knowledge_to must be after knowledge_from")
    digest = str(row.get("state_sha256") or "")
    if not _DIGEST.fullmatch(digest) or digest not in objects:
        raise RetrospectiveIntegrityError(
            "history interval references an unknown state SHA-256")
    previous = row.get("previous_state_sha256")
    if previous is not None and (
            not _DIGEST.fullmatch(str(previous)) or str(previous) not in objects):
        raise RetrospectiveIntegrityError(
            "history interval references an unknown previous state")
    if row.get("text_status") not in _TEXT_STATUSES:
        raise RetrospectiveIntegrityError("unsupported text_status")
    if row.get("text_status") == "derived_verified" and (
            row.get("body_complete") is not True
            or row.get("source_exact") is not False
            or row.get("reverse_replay_verified") is not True):
        raise RetrospectiveIntegrityError(
            "derived_verified interval lacks complete replay proof")
    if row.get("date_status") not in _DATE_STATUSES:
        raise RetrospectiveIntegrityError("unsupported date_status")
    if effective_from is not None and row.get("date_basis") == \
            "retrieval_observation_not_effective_date":
        raise RetrospectiveIntegrityError(
            "retrieval date cannot be used as effective_from")
    published_at = _legal_date(
        row.get("published_at"), "published_at", optional=True)
    observed_at = _legal_date(
        row.get("observed_at"), "observed_at", optional=True)
    verified_through = _legal_date(
        row.get("verified_through_observed_at"),
        "verified_through_observed_at", optional=True)
    if effective_to is None and verified_through is None:
        raise RetrospectiveIntegrityError(
            "open legal interval has no verified observation ceiling")
    provenance = row.get("provenance") or {}
    if not isinstance(provenance, dict):
        raise RetrospectiveIntegrityError("provenance must be an object")
    gaps = row.get("gaps") or []
    if not isinstance(gaps, list) or any(
            not isinstance(gap, dict) for gap in gaps):
        raise RetrospectiveIntegrityError("gaps must be a list of objects")
    # German legislation can expressly have retroactive effect.  Preserve it
    # instead of rejecting the official date merely because it precedes
    # publication; expose that fact explicitly to clients.
    row["retroactive"] = bool(
        published_at and effective_from and effective_from < published_at)
    row.update({
        "effective_from": effective_from,
        "effective_to": effective_to,
        "knowledge_from": knowledge_from.isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "knowledge_to": (knowledge_to.isoformat(
            timespec="seconds").replace("+00:00", "Z")
            if knowledge_to else None),
        "published_at": published_at,
        "observed_at": observed_at,
        "verified_through_observed_at": verified_through,
        "state_sha256": digest,
        "act_id": act_id,
        "evidence": _validate_evidence(row.get("evidence") or []),
        "gaps": list(gaps),
        "provenance": dict(provenance),
    })
    return row


def _validate_event(raw: Any, act_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RetrospectiveIntegrityError("amendment event must be an object")
    row = dict(raw)
    assertion_id = str(row.get("id") or "")
    if not assertion_id:
        raise RetrospectiveIntegrityError("amendment event has no assertion id")
    if row.get("act_id") not in (None, act_id):
        raise RetrospectiveIntegrityError("amendment event act_id mismatch")
    published = _legal_date(row.get("published_at"), "published_at")
    effective = _legal_date(
        row.get("effective_at"), "effective_at", optional=True)
    display_date = _legal_date(row.get("date"), "date")
    if display_date != (effective or published):
        raise RetrospectiveIntegrityError(
            "event display date contradicts effective/published dates")
    observed = _legal_date(
        row.get("observed_at"), "observed_at", optional=True)
    ingested = _instant(
        row.get("ingested_at"), "ingested_at", optional=True)
    knowledge_from = _instant(row.get("knowledge_from"), "knowledge_from")
    knowledge_to = _instant(
        row.get("knowledge_to"), "knowledge_to", optional=True)
    assert knowledge_from is not None
    if knowledge_to is not None and knowledge_to <= knowledge_from:
        raise RetrospectiveIntegrityError(
            "event knowledge_to must be after knowledge_from")
    if row.get("text_status") not in _EVENT_TEXT_STATUSES:
        raise RetrospectiveIntegrityError("unsupported event text_status")
    expected_date_status = "official_verified" if effective else "unknown"
    if row.get("date_status") != expected_date_status:
        raise RetrospectiveIntegrityError(
            "event date_status contradicts effective_at")
    for field in ("affected_norms", "commands", "gaps"):
        if not isinstance(row.get(field, []), list):
            raise RetrospectiveIntegrityError(
                f"event {field} must be a list")
    pdf_sha = row.get("pdf_sha256")
    if pdf_sha is not None and not _DIGEST.fullmatch(str(pdf_sha)):
        raise RetrospectiveIntegrityError("event has invalid PDF SHA-256")
    if row.get("candidate_only") is not True or \
            row.get("historical_text_reconstructed") is not False:
        raise RetrospectiveIntegrityError(
            "event must not claim a reconstructed historical state")
    if observed is not None and observed < published:
        raise RetrospectiveIntegrityError(
            "event was allegedly observed before publication")
    row.update({
        "id": assertion_id,
        "act_id": act_id,
        "published_at": published,
        "effective_at": effective,
        "date": display_date,
        "observed_at": observed,
        "ingested_at": (ingested.isoformat(timespec="seconds").replace(
            "+00:00", "Z") if ingested else None),
        "knowledge_from": knowledge_from.isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "knowledge_to": (knowledge_to.isoformat(
            timespec="seconds").replace("+00:00", "Z")
            if knowledge_to else None),
        "retroactive": bool(effective and effective < published),
        "affected_norms": list(row.get("affected_norms") or []),
        "commands": list(row.get("commands") or []),
        "gaps": list(row.get("gaps") or []),
        "evidence": _validate_evidence(row.get("evidence") or []),
    })
    return row


def _validate_observation(raw: Any, objects: dict[str, Any],
                          act_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RetrospectiveIntegrityError("state observation must be an object")
    row = dict(raw)
    if row.get("act_id") != act_id:
        raise RetrospectiveIntegrityError("state observation act_id mismatch")
    observed = _legal_date(row.get("observed_at"), "observed_at")
    digest = str(row.get("state_sha256") or "")
    if not _DIGEST.fullmatch(digest) or digest not in objects:
        raise RetrospectiveIntegrityError(
            "state observation references an unknown SHA-256")
    source = str(row.get("source_url") or "")
    if not source.startswith("https://www.gesetze-im-internet.de/"):
        raise RetrospectiveIntegrityError(
            "state observation source is not official GII")
    row["observed_at"] = observed
    row["state_sha256"] = digest
    return row


def _legal_interval_contains(row: dict[str, Any], point: str) -> bool:
    if not _interval_contains(
            row["effective_from"], row.get("effective_to"), point,
            dates=True):
        return False
    if row.get("effective_to") is not None:
        return True
    through = _legal_date(
        row.get("verified_through_observed_at"),
        "verified_through_observed_at")
    requested = _legal_date(point, "requested legal date")
    assert through is not None and requested is not None
    return requested <= through


def _overlap_end(row: dict[str, Any]) -> date:
    if row.get("effective_to") is not None:
        parsed = _legal_date(row["effective_to"], "effective_to")
        assert parsed is not None
        return date.fromisoformat(parsed)
    through = _legal_date(
        row.get("verified_through_observed_at"),
        "verified_through_observed_at")
    assert through is not None
    parsed = date.fromisoformat(through)
    return parsed + timedelta(days=1) if parsed < date.max else date.max


def validate_manifest(raw: Any) -> dict[str, Any]:
    """Validate and normalise one retrospective-history manifest."""
    if not isinstance(raw, dict):
        raise RetrospectiveIntegrityError("retrospective manifest must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION or raw.get("kind") != KIND:
        raise RetrospectiveIntegrityError("unsupported retrospective schema")
    built_at = _normalised_instant(raw.get("built_at"), "built_at")
    built_instant = _instant(built_at, "built_at")
    assert built_instant is not None
    objects = raw.get("objects")
    acts = raw.get("acts")
    if not isinstance(objects, dict) or not isinstance(acts, dict):
        raise RetrospectiveIntegrityError("manifest objects/acts have wrong shape")
    for digest, metadata in objects.items():
        if not _DIGEST.fullmatch(str(digest)) or not isinstance(metadata, dict):
            raise RetrospectiveIntegrityError("invalid state object metadata")
        gzip_sha = metadata.get("gzip_sha256")
        if gzip_sha is not None and not _DIGEST.fullmatch(str(gzip_sha)):
            raise RetrospectiveIntegrityError(
                "invalid compressed state object SHA-256")

    validated_acts: dict[str, dict[str, Any]] = {}
    assertion_ids: set[str] = set()
    for act_id, raw_act in acts.items():
        if not isinstance(act_id, str) or not act_id or not isinstance(raw_act, dict):
            raise RetrospectiveIntegrityError("invalid act history entry")
        if raw_act.get("act_id") != act_id:
            raise RetrospectiveIntegrityError("act history key/id mismatch")
        intervals = [_validate_interval(row, objects, act_id)
                     for row in raw_act.get("intervals") or []]
        events = [_validate_event(row, act_id)
                  for row in raw_act.get("events") or []]
        observations = [_validate_observation(row, objects, act_id)
                        for row in raw_act.get("observations") or []]
        for row in [*intervals, *events]:
            assertion_id = row["id"]
            if assertion_id in assertion_ids:
                raise RetrospectiveIntegrityError(
                    f"duplicate assertion id: {assertion_id}")
            assertion_ids.add(assertion_id)
            knowledge_from = _instant(
                row["knowledge_from"], "knowledge_from")
            knowledge_to = _instant(
                row.get("knowledge_to"), "knowledge_to", optional=True)
            assert knowledge_from is not None
            if knowledge_from > built_instant or (
                    knowledge_to is not None and
                    knowledge_to > built_instant):
                raise RetrospectiveIntegrityError(
                    "assertion knowledge interval exceeds built_at")
        # Overlap is legal across successive knowledge assertions, but never
        # at one knowledge instant.  Checking every boundary is sufficient for
        # half-open intervals and avoids a quadratic time grid.
        knowledge_points = sorted({
            instant for row in intervals
            for instant in (row["knowledge_from"], row.get("knowledge_to"))
            if instant is not None
        })
        if not knowledge_points:
            knowledge_points = [built_at]
        for point in knowledge_points:
            visible = [row for row in intervals if _interval_contains(
                row["knowledge_from"], row.get("knowledge_to"), point,
                dates=False)]
            for index, left in enumerate(visible):
                left_start = date.fromisoformat(left["effective_from"])
                left_end = _overlap_end(left)
                for right in visible[index + 1:]:
                    right_start = date.fromisoformat(right["effective_from"])
                    right_end = _overlap_end(right)
                    if left_start < right_end and right_start < left_end:
                        raise RetrospectiveAmbiguity(
                            f"overlapping legal intervals for {act_id} at {point}")
        act = dict(raw_act)
        act["intervals"] = sorted(intervals, key=lambda row: (
            row["effective_from"], row["knowledge_from"], str(row.get("id") or "")))
        gaps = act.get("gaps") or []
        if not isinstance(gaps, list) or any(
                not isinstance(gap, dict) for gap in gaps):
            raise RetrospectiveIntegrityError(
                "act gaps must be a list of objects")
        act["events"] = sorted(events, key=lambda row: (
            row["published_at"], row["knowledge_from"], row["id"]))
        act["observations"] = sorted(observations, key=lambda row: (
            row["observed_at"], row["state_sha256"]))
        act["gaps"] = list(gaps)
        validated_acts[act_id] = act
    result = dict(raw)
    result["built_at"] = built_at
    result["acts"] = validated_acts
    result["objects"] = dict(objects)
    counts = raw.get("counts")
    if counts is not None:
        if not isinstance(counts, dict):
            raise RetrospectiveIntegrityError("counts must be an object")
        expected = {
            "acts": len(validated_acts),
            "interval_assertions": sum(
                len(act["intervals"]) for act in validated_acts.values()),
            "current_intervals": sum(
                row.get("knowledge_to") is None
                for act in validated_acts.values()
                for row in act["intervals"]),
            "events": sum(
                len(act["events"]) for act in validated_acts.values()),
            "current_events": sum(
                row.get("knowledge_to") is None
                for act in validated_acts.values()
                for row in act["events"]),
            "events_with_effective_date": sum(
                bool(row.get("effective_at"))
                for act in validated_acts.values()
                for row in act["events"]
                if row.get("knowledge_to") is None),
            "observations": sum(
                len(act["observations"]) for act in validated_acts.values()),
            "state_objects": len(objects),
        }
        if any(counts.get(key) != value for key, value in expected.items()):
            raise RetrospectiveIntegrityError(
                "manifest counts do not match validated rows")
        result["counts"] = dict(counts)
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrospectiveIntegrityError(
            f"cannot read retrospective manifest: {exc}") from exc
    return validate_manifest(raw)


def resolve_as_of(manifest: dict[str, Any], value: Any = None) -> str:
    result = (_normalised_instant(value, "as_of") if value is not None
              else str(manifest["built_at"]))
    requested = _instant(result, "as_of")
    built = _instant(manifest.get("built_at"), "built_at")
    assert requested is not None and built is not None
    if requested > built:
        raise RetrospectiveNotFound(
            "as_of is beyond the manifest knowledge horizon")
    return result


def act_history(manifest: dict[str, Any], act_id: str,
                *, as_of: Any = None) -> dict[str, Any]:
    try:
        raw = manifest["acts"][act_id]
    except KeyError as exc:
        raise RetrospectiveNotFound(
            f"no retrospective history for {act_id}") from exc
    knowledge = resolve_as_of(manifest, as_of)
    result = dict(raw)
    result["as_of"] = knowledge
    result["intervals"] = [row for row in raw.get("intervals") or []
                           if _interval_contains(
                               row["knowledge_from"], row.get("knowledge_to"),
                               knowledge, dates=False)]
    result["events"] = [row for row in raw.get("events") or []
                        if _event_visible(row, knowledge)]
    return result


def _event_norm_matches(row: dict[str, Any], norm: str) -> bool:
    """Return whether an event explicitly targets one norm designator."""
    requested = _norm_token(norm)
    for label in row.get("affected_norms") or []:
        candidate = _norm_token(label)
        if candidate == requested or (
                requested[0] is None and candidate[1] == requested[1]):
            return True
    for command in row.get("commands") or []:
        if not isinstance(command, dict):
            continue
        reference = command.get("ref") or {}
        if not isinstance(reference, dict):
            continue
        candidates = []
        if reference.get("para") is not None:
            candidates.append(("section", str(reference["para"]).casefold()))
        if reference.get("article") is not None:
            candidates.append(("article", str(reference["article"]).casefold()))
        if requested in candidates or (
                requested[0] is None and any(
                    candidate[1] == requested[1] for candidate in candidates)):
            return True
    return False


def list_changes(manifest: dict[str, Any], *, as_of: Any = None,
                 act: str | None = None, norm: str | None = None,
                 from_date: Any = None, to_date: Any = None,
                 future: bool | None = None, reference_date: Any = None,
                 query: str | None = None) -> dict[str, Any]:
    """List visible amendment events across acts at one knowledge-time slice.

    These are evidence-bound amendment commands, not reconstructed complete
    historical states.  Publication and legal-effect dates remain separate.
    """
    knowledge = resolve_as_of(manifest, as_of)
    lower = (_legal_date(from_date, "from", optional=True)
             if from_date is not None else None)
    upper = (_legal_date(to_date, "to", optional=True)
             if to_date is not None else None)
    if lower and upper and lower > upper:
        raise RetrospectiveIntegrityError("from must not be after to")
    today = _legal_date(
        reference_date if reference_date is not None else date.today(),
        "reference_date")
    assert today is not None
    act_needle = str(act or "").strip().casefold()
    query_needle = " ".join(str(query or "").casefold().split())
    rows = []
    for act_id, history in manifest.get("acts", {}).items():
        if act_needle and act_needle not in {
                act_id.casefold(),
                str(history.get("jurabk") or "").casefold()}:
            continue
        for raw in history.get("events") or []:
            if not _event_visible(raw, knowledge):
                continue
            row = {
                **raw,
                "act_id": act_id,
                "jurabk": history.get("jurabk"),
                "act_title": history.get("title"),
            }
            event_date = str(row.get("effective_at") or row["published_at"])
            if lower and event_date < lower:
                continue
            if upper and event_date > upper:
                continue
            is_future = bool(
                row.get("effective_at") and row["effective_at"] > today)
            if future is not None and is_future is not future:
                continue
            if norm and not _event_norm_matches(row, norm):
                continue
            if query_needle:
                haystack = " ".join(str(value or "") for value in (
                    row.get("document_id"), row.get("procedure_title"),
                    row.get("article_heading"), row.get("jurabk"),
                    row.get("act_title"), " ".join(row.get("affected_norms") or []),
                    " ".join(str(command.get("raw") or "")
                             for command in row.get("commands") or []
                             if isinstance(command, dict)),
                )).casefold()
                if query_needle not in haystack:
                    continue
            row["future"] = is_future
            rows.append(row)
    rows.sort(key=lambda row: (
        str(row.get("effective_at") or row.get("published_at") or ""),
        str(row.get("published_at") or ""), str(row.get("document_id") or ""),
        str(row.get("amending_article") or ""), str(row.get("act_id") or ""),
    ), reverse=True)
    return {
        "schema_version": manifest.get("schema_version"),
        "kind": "lexgraph-amendment-events",
        "built_at": manifest.get("built_at"),
        "as_of": knowledge,
        "reference_date": today,
        "source_policy": manifest.get("source_policy"),
        "matched": len(rows),
        "events": rows,
    }


def amending_act(manifest: dict[str, Any], document_id: str, *,
                 as_of: Any = None) -> dict[str, Any]:
    """Aggregate all visible per-act events belonging to one BGBl document."""
    document = str(document_id or "").strip()
    if not document:
        raise RetrospectiveNotFound("amending document id is empty")
    changes = list_changes(manifest, as_of=as_of)
    events = [row for row in changes["events"]
              if str(row.get("document_id") or "") == document]
    if not events:
        raise RetrospectiveNotFound(
            f"no visible amendment events for {document}")

    evidence = _dedupe([
        item for row in events for item in row.get("evidence") or []])
    affected_acts = []
    for act_id in sorted({str(row["act_id"]) for row in events}):
        act_events = [row for row in events if row["act_id"] == act_id]
        affected_acts.append({
            "act_id": act_id,
            "jurabk": act_events[0].get("jurabk"),
            "title": act_events[0].get("act_title"),
            "event_count": len(act_events),
            "command_count": sum(len(row.get("commands") or [])
                                 for row in act_events),
            "affected_norms": sorted({
                norm for row in act_events
                for norm in row.get("affected_norms") or []}),
        })

    articles = []
    article_keys = sorted({(
        str(row.get("amending_article") or ""),
        str(row.get("article_heading") or "")) for row in events},
        key=lambda key: (int(key[0]) if key[0].isdigit() else 10**9,
                         key[0], key[1]))
    for article, heading in article_keys:
        article_events = [row for row in events if (
            str(row.get("amending_article") or ""),
            str(row.get("article_heading") or "")) == (article, heading)]
        articles.append({
            "article": article or None,
            "heading": heading or None,
            "affected_acts": sorted({row["act_id"] for row in article_events}),
            "affected_norms": sorted({
                norm for row in article_events
                for norm in row.get("affected_norms") or []}),
            "command_count": sum(len(row.get("commands") or [])
                                 for row in article_events),
            "events": article_events,
        })

    def unique(field: str) -> list[Any]:
        return sorted({row[field] for row in events if row.get(field)})

    publications = unique("published_at")
    procedures = unique("procedure_id")
    procedure_titles = unique("procedure_title")
    pdf_hashes = unique("pdf_sha256")
    return {
        "schema_version": manifest.get("schema_version"),
        "kind": "lexgraph-amending-act",
        "built_at": manifest.get("built_at"),
        "as_of": changes["as_of"],
        "document_id": document,
        "published_at": publications[0] if len(publications) == 1 else None,
        "published_dates": publications,
        "procedure_id": procedures[0] if len(procedures) == 1 else None,
        "procedure_ids": procedures,
        "title": procedure_titles[0] if len(procedure_titles) == 1 else None,
        "titles": procedure_titles,
        "official_html_url": next((row.get("official_html_url") for row in events
                                   if row.get("official_html_url")), None),
        "official_pdf_url": next((row.get("official_pdf_url") for row in events
                                  if row.get("official_pdf_url")), None),
        "pdf_sha256": pdf_hashes[0] if len(pdf_hashes) == 1 else None,
        "pdf_sha256s": pdf_hashes,
        "event_count": len(events),
        "command_count": sum(len(row.get("commands") or []) for row in events),
        "affected_acts": affected_acts,
        "articles": articles,
        "evidence": evidence,
        "source_policy": manifest.get("source_policy"),
        "historical_text_reconstructed": False,
    }


def _event_visible(row: Any, as_of: str) -> bool:
    if not isinstance(row, dict):
        return False
    start = row.get("knowledge_from")
    end = row.get("knowledge_to")
    if start is None:
        return True
    return _interval_contains(start, end, as_of, dates=False)


def resolve_interval(manifest: dict[str, Any], act_id: str, at: Any,
                     *, as_of: Any = None) -> dict[str, Any]:
    requested = _legal_date(at, "at")
    knowledge = resolve_as_of(manifest, as_of)
    history = act_history(manifest, act_id, as_of=knowledge)
    matches = [row for row in history["intervals"]
               if _legal_interval_contains(row, requested)]
    if not matches:
        raise RetrospectiveNotFound(
            f"no asserted state for {act_id} at {requested} as known {knowledge}")
    if len(matches) != 1:
        raise RetrospectiveAmbiguity(
            f"ambiguous state for {act_id} at {requested} as known {knowledge}")
    return {
        **matches[0],
        "act_id": act_id,
        "jurabk": history.get("jurabk"),
        "requested_at": requested,
        "as_of": knowledge,
    }


def load_interval_state(interval: dict[str, Any],
                        store: Path) -> dict[str, Any]:
    try:
        state = load_state_digest(
            store, str(interval["state_sha256"]),
            act_id=str(interval.get("act_id") or "") or None,
            jurabk=str(interval.get("jurabk") or "") or None)
    except (KeyError, OfficialStateError) as exc:
        raise RetrospectiveIntegrityError(
            f"cannot verify retrospective state: {exc}") from exc
    return state


def _norm_token(value: Any) -> tuple[str | None, str]:
    raw = " ".join(str(value or "").split()).casefold()
    if match := re.match(r"^§+\s*(\d+[a-z]*)\b", raw):
        return "section", match.group(1)
    if match := re.match(r"^(?:art(?:ikel)?\.?)\s*(\d+[a-z]*)\b", raw):
        return "article", match.group(1)
    if match := re.match(r"^(\d+[a-z]*)$", raw):
        return None, match.group(1)
    return "label", raw


def _select_norms(state: dict[str, Any], norm: str | None
                  ) -> dict[tuple[str | None, str], dict[str, Any]]:
    rows: dict[tuple[str | None, str], dict[str, Any]] = {}
    for raw in state.get("norms") or []:
        if not isinstance(raw, dict):
            continue
        key = _norm_token(raw.get("enbez"))
        if key in rows:
            raise RetrospectiveIntegrityError(
                f"duplicate norm designator in state: {raw.get('enbez')}")
        rows[key] = raw
    if norm is None:
        return rows
    requested = _norm_token(norm)
    matches = {key: row for key, row in rows.items()
               if key == requested or (
                   requested[0] is None and key[1] == requested[1])}
    if len(matches) != 1:
        raise RetrospectiveNotFound(
            f"norm {norm!r} is missing or ambiguous in the selected state")
    return matches


def _norm_digest(row: dict[str, Any] | None) -> str | None:
    return (hashlib.sha256(json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()
            if row is not None else None)


def diff_intervals(manifest: dict[str, Any], act_id: str,
                   from_date: Any, to_date: Any, store: Path, *,
                   as_of: Any = None, norm: str | None = None) -> dict[str, Any]:
    """Return a content diff at one bitemporal knowledge slice."""
    left = resolve_interval(manifest, act_id, from_date, as_of=as_of)
    right = resolve_interval(manifest, act_id, to_date, as_of=left["as_of"])
    left_state = load_interval_state(left, store)
    right_state = load_interval_state(right, store)
    before = _select_norms(left_state, norm)
    after = _select_norms(right_state, norm)
    changes = []
    for key in sorted(set(before) | set(after), key=lambda item: (
            item[0] or "", item[1])):
        old = before.get(key)
        new = after.get(key)
        old_digest = _norm_digest(old)
        new_digest = _norm_digest(new)
        if old_digest == new_digest:
            continue
        operation = "add" if old is None else "remove" if new is None else "replace"
        changes.append({
            "operation": operation,
            "enbez": (new or old or {}).get("enbez"),
            "title": (new or old or {}).get("titel"),
            "old": (old or {}).get("text") if old is not None else None,
            "new": (new or {}).get("text") if new is not None else None,
            "old_sha256": old_digest,
            "new_sha256": new_digest,
        })
    gaps = _dedupe([*(left.get("gaps") or []), *(right.get("gaps") or [])])
    complete = (left.get("text_status") in {
                    "official_exact", "derived_verified"}
                and right.get("text_status") in {
                    "official_exact", "derived_verified"}
                and not gaps)
    source_exact = (left.get("text_status") == "official_exact"
                    and right.get("text_status") == "official_exact"
                    and not gaps)
    return {
        "schema_version": SCHEMA_VERSION,
        "act_id": act_id,
        "as_of": left["as_of"],
        "from": _interval_summary(left),
        "to": _interval_summary(right),
        "norm": norm,
        # ``exact`` retains its backwards-compatible meaning: both bodies are
        # exact official source snapshots.  A replay-verified derived body is
        # nevertheless complete and queryable, but never relabelled as source.
        "exact": source_exact,
        "source_exact": source_exact,
        "complete": complete,
        "verified_reconstruction": complete and not source_exact,
        "partial": not complete,
        "gaps": gaps,
        "changes": changes,
    }


def _interval_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in (
        "requested_at", "effective_from", "effective_to", "published_at",
        "observed_at", "knowledge_from", "knowledge_to", "state_sha256",
        "text_status", "date_status", "date_basis", "verification",
        "retroactive", "body_complete", "source_exact",
        "reverse_replay_verified", "verified_through_observed_at",
    )}


def _dedupe(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result = []
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
