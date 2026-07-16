"""Fail-closed bitemporal history over official Lexgraph state evidence.

The module deliberately keeps the two clocks separate:

``effective_*``
    Valid time: when a consolidated version applied as law.  These fields are
    populated only by an accepted BGBl/DIP transition review.

``knowledge_*`` / ``observed_at``
    Transaction time: when Lexgraph first saw a complete GII state and when a
    materialized assertion was superseded by stronger official evidence.

An observation is never promoted to an effective date.  Full act/norm bodies
remain in the existing SHA-256 CAS; this manifest contains verified references
only.  :func:`checkout_at` and :func:`diff_between` take the CAS objects as an
explicit mapping and verify every object before returning text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
KIND = "lexgraph-retrospective-bitemporal-history"
PUBLIC_KIND = "lexgraph-retrospective-history"
OBSERVATION_DATE_BASIS = "retrieval_observation_not_effective_date"
OBSERVATION_VERIFICATION = "exact"
REVIEW_DATE_BASIS = "official_bgbl_command_and_commencement_clause"
REVIEW_VERIFICATION = "official_final_text_and_complete_state_pair"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class RetrospectiveHistoryError(ValueError):
    """Evidence is incomplete, ambiguous, corrupt, or outside coverage."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical JSON representation used by the official CAS."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RetrospectiveHistoryError(
            f"value is not canonical JSON: {exc}") from exc


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    return _sha256(canonical_json_bytes(value))


def _iso_day(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise RetrospectiveHistoryError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RetrospectiveHistoryError(
            f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise RetrospectiveHistoryError(f"{field} must be YYYY-MM-DD")
    return value


def _valid_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise RetrospectiveHistoryError(f"{field} must be a SHA-256 digest")
    return value


def _json_copy(value: Any) -> Any:
    """Copy and simultaneously reject non-JSON values and NaN/Infinity."""
    return json.loads(canonical_json_bytes(value))


def _validate_state(digest: str, raw: Any) -> dict[str, Any]:
    digest = _valid_digest(digest, "state_sha256")
    state = _json_copy(raw)
    if not isinstance(state, dict):
        raise RetrospectiveHistoryError(f"state {digest} must be an object")
    if _digest(state) != digest:
        raise RetrospectiveHistoryError(
            f"state object hash mismatch for {digest}")
    for key in ("id", "jurabk", "juris", "title"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise RetrospectiveHistoryError(
                f"state {digest} has invalid {key}")
    norms = state.get("norms")
    if not isinstance(norms, list):
        raise RetrospectiveHistoryError(f"state {digest} has no norm list")
    if isinstance(state.get("norm_count"), bool) or \
            not isinstance(state.get("norm_count"), int) or \
            state["norm_count"] != len(norms):
        raise RetrospectiveHistoryError(
            f"state {digest} norm_count does not match norms")
    for index, norm in enumerate(norms):
        if not isinstance(norm, dict):
            raise RetrospectiveHistoryError(
                f"state {digest} norm {index} must be an object")
        for key in ("enbez", "titel", "text", "glied"):
            if not isinstance(norm.get(key), str):
                raise RetrospectiveHistoryError(
                    f"state {digest} norm {index} has invalid {key}")
    return state


def _normalise_observations(
        rows: Iterable[dict[str, Any]],
        states: Mapping[str, Any],
        ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    validated_states: dict[str, dict[str, Any]] = {}
    unique: dict[bytes, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise RetrospectiveHistoryError("observation must be an object")
        row = _json_copy(raw)
        for field in ("act_id", "jurabk", "builddate", "source_url",
                      "date_basis", "verification"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise RetrospectiveHistoryError(
                    f"observation has invalid {field}")
        row["observed_at"] = _iso_day(
            row.get("observed_at"), "observed_at")
        digest = _valid_digest(row.get("state_sha256"), "state_sha256")
        if row["date_basis"] != OBSERVATION_DATE_BASIS:
            raise RetrospectiveHistoryError(
                "observation date basis is not retrieval-only")
        if row["verification"] != OBSERVATION_VERIFICATION:
            raise RetrospectiveHistoryError(
                "observation is not an exact complete state")
        if not row["source_url"].startswith(
                "https://www.gesetze-im-internet.de/"):
            raise RetrospectiveHistoryError(
                "observation source is not official GII")
        if isinstance(row.get("norm_count"), bool) or \
                not isinstance(row.get("norm_count"), int) or \
                row["norm_count"] < 0:
            raise RetrospectiveHistoryError(
                "observation has invalid norm_count")
        if digest not in states:
            raise RetrospectiveHistoryError(
                f"observation references missing state {digest}")
        state = validated_states.setdefault(
            digest, _validate_state(digest, states[digest]))
        if state["id"] != row["act_id"] or \
                state["jurabk"] != row["jurabk"] or \
                state["norm_count"] != row["norm_count"]:
            raise RetrospectiveHistoryError(
                "observation metadata does not match state object")
        unique[canonical_json_bytes(row)] = row

    observations = sorted(unique.values(), key=lambda row: (
        row["act_id"], row["observed_at"], row["state_sha256"],
        row["builddate"],
    ))
    by_act_day: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    act_names: dict[str, str] = {}
    for row in observations:
        key = (row["act_id"], row["observed_at"])
        by_act_day[key].append(row)
        prior_name = act_names.setdefault(row["act_id"], row["jurabk"])
        if prior_name != row["jurabk"]:
            raise RetrospectiveHistoryError(
                f"act {row['act_id']} has conflicting jurabk values")
    for (act_id, day), same_day in by_act_day.items():
        # Date-only observations cannot order even two byte-identical records
        # carrying different build/source metadata.  Keep the boundary strict.
        if len(same_day) != 1:
            raise RetrospectiveHistoryError(
                f"{act_id} has ambiguous observations on {day}")
    return observations, validated_states


def _norm_groups(state: dict[str, Any]) -> tuple[list[str],
                                                       dict[str, list[dict]]]:
    labels: list[str] = []
    groups: dict[str, list[dict]] = defaultdict(list)
    for norm in state["norms"]:
        label = norm["enbez"]
        if label not in groups:
            labels.append(label)
        groups[label].append(norm)
    return labels, groups


def _official_state_changes(old: dict[str, Any],
                            new: dict[str, Any]) -> list[dict[str, Any]]:
    """Mirror the official adjacent-state diff, including duplicate labels."""
    old_labels, old_groups = _norm_groups(old)
    new_labels, new_groups = _norm_groups(new)
    labels = old_labels + [label for label in new_labels
                           if label not in old_groups]
    pairs: list[tuple[str, dict | None, dict | None]] = []
    for label in labels:
        before = list(old_groups.get(label, []))
        remaining_after = list(new_groups.get(label, []))
        remaining_before = []
        for row in before:
            try:
                match_at = remaining_after.index(row)
            except ValueError:
                remaining_before.append(row)
            else:
                remaining_after.pop(match_at)
        paired = min(len(remaining_before), len(remaining_after))
        pairs.extend((label, remaining_before[index], remaining_after[index])
                     for index in range(paired))
        pairs.extend((label, row, None) for row in remaining_before[paired:])
        pairs.extend((label, None, row) for row in remaining_after[paired:])

    changes: list[dict[str, Any]] = []
    for label, before, after in pairs:
        old_text = before["text"] if before else ""
        new_text = after["text"] if after else ""
        changes.append({
            "para": label,
            "old": old_text,
            "new": new_text,
            "old_present": before is not None,
            "new_present": after is not None,
            "old_sha256": _sha256(old_text),
            "new_sha256": _sha256(new_text),
            "operation": ("add" if before is None else
                          "delete" if after is None else "replace"),
            "old_title": before["titel"] if before else None,
            "new_title": after["titel"] if after else None,
            "old_glied": before["glied"] if before else None,
            "new_glied": after["glied"] if after else None,
            "old_norm_sha256": _digest(before) if before else None,
            "new_norm_sha256": _digest(after) if after else None,
        })
    return changes


def _changes_marker(changes: Any) -> list[bytes]:
    if not isinstance(changes, list) or not changes:
        raise RetrospectiveHistoryError(
            "accepted transition review must contain exact changes")
    required = {
        "para", "old", "new", "old_present", "new_present",
        "old_sha256", "new_sha256", "operation", "old_title",
        "new_title", "old_glied", "new_glied", "old_norm_sha256",
        "new_norm_sha256",
    }
    markers = []
    for change in changes:
        if not isinstance(change, dict) or not required <= change.keys():
            raise RetrospectiveHistoryError(
                "accepted review has an incomplete state change")
        markers.append(canonical_json_bytes(
            {key: change[key] for key in sorted(required)}))
    return sorted(markers)


def _segments(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for row in observations:
        if not segments or \
                segments[-1]["state_sha256"] != row["state_sha256"]:
            segments.append({
                "index": len(segments),
                "state_sha256": row["state_sha256"],
                "observations": [row],
            })
        else:
            segments[-1]["observations"].append(row)
    return segments


def _review_has_official_evidence(review: dict[str, Any]) -> bool:
    bgbl = review.get("bgbl")
    derivation = review.get("derivation")
    evidence = review.get("evidence")
    if not isinstance(bgbl, dict) or bgbl.get("integrity_verified") is not True:
        return False
    try:
        _valid_digest(bgbl.get("pdf_sha256"), "bgbl.pdf_sha256")
    except RetrospectiveHistoryError:
        return False
    if not isinstance(derivation, dict) or \
            derivation.get("effective_dates_inferred") is not False:
        return False
    if not isinstance(evidence, list):
        return False
    sources = {str(row.get("source") or "").upper()
               for row in evidence if isinstance(row, dict)}
    if "BUZER" in sources:
        return False
    gii_hashes = {row.get("state_sha256") for row in evidence
                  if isinstance(row, dict) and
                  str(row.get("source") or "").upper() == "GII"}
    return {"GII", "BGBL", "DIP"} <= sources and \
        review.get("previous_state_sha256") in gii_hashes and \
        review.get("state_sha256") in gii_hashes


def _normalise_reviews(
        rows: Iterable[dict[str, Any]],
        observations_by_act: Mapping[str, list[dict[str, Any]]],
        validated_states: Mapping[str, dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
    reviews: list[dict[str, Any]] = []
    by_boundary: dict[tuple[str, int], dict[str, Any]] = {}
    exact_seen: set[bytes] = set()
    review_ids: set[str] = set()
    segment_cache = {act_id: _segments(act_rows)
                     for act_id, act_rows in observations_by_act.items()}

    for raw in rows:
        if not isinstance(raw, dict):
            raise RetrospectiveHistoryError(
                "accepted transition review must be an object")
        review = _json_copy(raw)
        marker = canonical_json_bytes(review)
        if marker in exact_seen:
            continue
        exact_seen.add(marker)
        for field in ("id", "act_id", "jurabk", "date_basis",
                      "verification"):
            if not isinstance(review.get(field), str) or not review[field]:
                raise RetrospectiveHistoryError(
                    f"accepted review has invalid {field}")
        if review["id"] in review_ids:
            raise RetrospectiveHistoryError(
                f"duplicate accepted review id: {review['id']}")
        review_ids.add(review["id"])
        if review["date_basis"] != REVIEW_DATE_BASIS or \
                review["verification"] != REVIEW_VERIFICATION:
            raise RetrospectiveHistoryError(
                "transition review did not pass the official acceptance gate")
        published = _iso_day(review.get("published_at"), "published_at")
        effective = _iso_day(review.get("effective_at"), "effective_at")
        observed = _iso_day(review.get("observed_at"), "observed_at")
        previous_observed = _iso_day(
            review.get("previous_observed_at"), "previous_observed_at")
        if published > observed:
            raise RetrospectiveHistoryError(
                "official state was observed before publication")
        if previous_observed >= observed:
            raise RetrospectiveHistoryError(
                "review observation interval is empty or reversed")
        new_digest = _valid_digest(
            review.get("state_sha256"), "state_sha256")
        old_digest = _valid_digest(
            review.get("previous_state_sha256"),
            "previous_state_sha256")
        if new_digest == old_digest:
            raise RetrospectiveHistoryError(
                "accepted transition does not change the state")
        if old_digest not in validated_states or \
                new_digest not in validated_states:
            raise RetrospectiveHistoryError(
                "accepted review references a state without an observation")
        if not _review_has_official_evidence(review):
            raise RetrospectiveHistoryError(
                "accepted review lacks verified GII/BGBl/DIP evidence")

        act_id = review["act_id"]
        segments = segment_cache.get(act_id)
        if not segments:
            raise RetrospectiveHistoryError(
                f"accepted review references unknown act {act_id}")
        boundary_index = next((index for index in range(1, len(segments))
            if segments[index - 1]["state_sha256"] == old_digest and
            segments[index]["state_sha256"] == new_digest and
            segments[index - 1]["observations"][-1]["observed_at"] ==
                previous_observed and
            segments[index]["observations"][0]["observed_at"] == observed),
            None)
        if boundary_index is None:
            raise RetrospectiveHistoryError(
                "accepted review is not an adjacent observed state pair")
        old_state = validated_states[old_digest]
        new_state = validated_states[new_digest]
        if old_state["id"] != act_id or new_state["id"] != act_id or \
                old_state["jurabk"] != review["jurabk"] or \
                new_state["jurabk"] != review["jurabk"]:
            raise RetrospectiveHistoryError(
                "accepted review metadata does not match state pair")
        expected_changes = _official_state_changes(old_state, new_state)
        if _changes_marker(review.get("changes")) != \
                _changes_marker(expected_changes):
            raise RetrospectiveHistoryError(
                "accepted review changes do not match complete state pair")

        boundary_key = (act_id, boundary_index)
        if boundary_key in by_boundary:
            raise RetrospectiveHistoryError(
                "multiple accepted reviews claim one state boundary")
        review["published_at"] = published
        review["effective_at"] = effective
        review["observed_at"] = observed
        review["previous_observed_at"] = previous_observed
        review["state_sha256"] = new_digest
        review["previous_state_sha256"] = old_digest
        # German legislation may expressly order genuine retroactive effect.
        # This is not a malformed chronology: retain both official dates and
        # label the relationship instead of silently discarding the review.
        review["retroactive"] = effective < published
        by_boundary[boundary_key] = review
        reviews.append(review)

    for act_id, segments in segment_cache.items():
        ordered = [by_boundary[(act_id, index)]
                   for index in range(1, len(segments))
                   if (act_id, index) in by_boundary]
        effective_days = [row["effective_at"] for row in ordered]
        if any(left >= right for left, right in
               zip(effective_days, effective_days[1:])):
            raise RetrospectiveHistoryError(
                f"{act_id} has ambiguous or reversed effective transitions")
    reviews.sort(key=lambda row: (
        row["act_id"], row["observed_at"], row["effective_at"], row["id"]))
    return reviews, by_boundary


def _date_status(incoming: dict | None, outgoing: dict | None) -> str:
    if incoming and outgoing:
        return "verified_legal_interval"
    if incoming:
        return "verified_legal_start_open_end"
    if outgoing:
        return "unknown_legal_start_verified_end"
    return "observation_only_no_legal_dates"


def _confidence(incoming: dict | None, outgoing: dict | None) -> str:
    if incoming and outgoing:
        return "official_exact_text_and_verified_legal_interval"
    if incoming:
        return "official_exact_text_and_verified_legal_start"
    if outgoing:
        return "official_exact_text_with_unknown_legal_start"
    return "official_exact_text_observation_only"


def _evidence_summary(review: dict[str, Any] | None) -> dict | None:
    if review is None:
        return None
    return {
        "review_id": review["id"],
        "published_at": review["published_at"],
        "effective_at": review["effective_at"],
        "date_basis": review["date_basis"],
        "verification": review["verification"],
        "procedure_id": review.get("procedure_id"),
        "bgbl": copy.deepcopy(review.get("bgbl")),
        "evidence": copy.deepcopy(review.get("evidence")),
    }


def _fact_for_segment(
        act_id: str, segment: dict[str, Any], segments: list[dict[str, Any]],
        epoch: str, boundary_reviews: Mapping[tuple[str, int], dict[str, Any]],
        ) -> dict[str, Any]:
    index = segment["index"]
    visible_observations = [row for row in segment["observations"]
                            if row["observed_at"] <= epoch]
    if not visible_observations:
        raise RetrospectiveHistoryError("internal invisible state segment")
    incoming = boundary_reviews.get((act_id, index)) if index > 0 else None
    outgoing = boundary_reviews.get((act_id, index + 1)) \
        if index + 1 < len(segments) else None
    if incoming and incoming["observed_at"] > epoch:
        incoming = None
    if outgoing and outgoing["observed_at"] > epoch:
        outgoing = None
    first = visible_observations[0]
    last = visible_observations[-1]
    previous_digest = (segments[index - 1]["state_sha256"]
                       if index > 0 else None)
    date_basis = (REVIEW_DATE_BASIS if incoming or outgoing
                  else OBSERVATION_DATE_BASIS)
    verification = (REVIEW_VERIFICATION if incoming or outgoing
                    else OBSERVATION_VERIFICATION)
    return {
        "segment_id": _sha256(canonical_json_bytes([
            act_id, first["observed_at"], segment["state_sha256"]]))[:24],
        "act_id": act_id,
        "state_sha256": segment["state_sha256"],
        "previous_state_sha256": previous_digest,
        "observed_at": first["observed_at"],
        "last_observed_at": last["observed_at"],
        "published_at": incoming["published_at"] if incoming else None,
        "effective_from": incoming["effective_at"] if incoming else None,
        "effective_to": outgoing["effective_at"] if outgoing else None,
        "verified_through_observed_at": last["observed_at"],
        "text_status": "official_exact_complete_state",
        "date_status": _date_status(incoming, outgoing),
        "date_basis": date_basis,
        "verification": verification,
        "confidence": _confidence(incoming, outgoing),
        "provenance": {
            "observation": {
                "source": "GII",
                "source_url": last["source_url"],
                "first_observed_at": first["observed_at"],
                "last_observed_at": last["observed_at"],
                "state_sha256": segment["state_sha256"],
            },
            "incoming_legal_review": _evidence_summary(incoming),
            "outgoing_legal_review": _evidence_summary(outgoing),
        },
    }


def _materialise_act_intervals(
        act_id: str, observations: list[dict[str, Any]],
        boundary_reviews: Mapping[tuple[str, int], dict[str, Any]],
        ) -> list[dict[str, Any]]:
    segments = _segments(observations)
    epochs = sorted({row["observed_at"] for row in observations})
    active: dict[str, tuple[bytes, dict[str, Any]]] = {}
    completed: list[dict[str, Any]] = []
    for epoch in epochs:
        visible = [segment for segment in segments
                   if segment["observations"][0]["observed_at"] <= epoch]
        for segment in visible:
            fact = _fact_for_segment(
                act_id, segment, segments, epoch, boundary_reviews)
            slot = fact["segment_id"]
            signature = canonical_json_bytes(fact)
            prior = active.get(slot)
            if prior and prior[0] == signature:
                continue
            if prior:
                closed = prior[1]
                closed["knowledge_to"] = epoch
                completed.append(closed)
            row = fact
            row["knowledge_from"] = epoch
            row["knowledge_to"] = None
            row["version_id"] = _sha256(canonical_json_bytes([
                slot, row["state_sha256"], row["effective_from"],
                row["effective_to"], epoch,
            ]))[:24]
            active[slot] = (signature, row)
    completed.extend(row for _signature, row in active.values())
    completed.sort(key=lambda row: (
        row["knowledge_from"], row["observed_at"], row["version_id"]))
    return completed


def _norm_intervals(
        intervals: list[dict[str, Any]],
        states: Mapping[str, dict[str, Any]],
        ) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for interval in intervals:
        state = states[interval["state_sha256"]]
        occurrences: dict[str, int] = defaultdict(int)
        for norm_index, norm in enumerate(state["norms"]):
            enbez = norm["enbez"]
            occurrence = occurrences[enbez]
            occurrences[enbez] += 1
            row = {
                "act_version_id": interval["version_id"],
                "act_id": interval["act_id"],
                "state_sha256": interval["state_sha256"],
                "enbez": enbez,
                "occurrence": occurrence,
                "norm_index": norm_index,
                "norm_sha256": _digest(norm),
                "title": norm["titel"],
                "observed_at": interval["observed_at"],
                "published_at": interval["published_at"],
                "effective_from": interval["effective_from"],
                "effective_to": interval["effective_to"],
                "knowledge_from": interval["knowledge_from"],
                "knowledge_to": interval["knowledge_to"],
                "verified_through_observed_at":
                    interval["verified_through_observed_at"],
                "text_status": interval["text_status"],
                "date_status": interval["date_status"],
                "date_basis": interval["date_basis"],
                "verification": interval["verification"],
                "confidence": interval["confidence"],
            }
            row["norm_version_id"] = _sha256(canonical_json_bytes([
                interval["version_id"], norm_index, row["norm_sha256"]]))[:24]
            output.append(row)
    return output


def _gaps_for_act(
        act_id: str, observations: list[dict[str, Any]],
        boundary_reviews: Mapping[tuple[str, int], dict[str, Any]],
        ) -> list[dict[str, Any]]:
    segments = _segments(observations)
    gaps: list[dict[str, Any]] = []
    if segments and (act_id, 0) not in boundary_reviews:
        gaps.append({
            "kind": "unknown_effective_start",
            "state_sha256": segments[0]["state_sha256"],
            "observed_at": segments[0]["observations"][0]["observed_at"],
            "reason": "no accepted incoming official transition review",
        })
    for index in range(1, len(segments)):
        if (act_id, index) in boundary_reviews:
            continue
        previous = segments[index - 1]
        current = segments[index]
        gaps.append({
            "kind": "unreviewed_state_transition",
            "previous_state_sha256": previous["state_sha256"],
            "state_sha256": current["state_sha256"],
            "previous_observed_at":
                previous["observations"][-1]["observed_at"],
            "observed_at": current["observations"][0]["observed_at"],
            "reason": "effective date is unknown; observation was not reused",
        })
    return gaps


def materialize_history(
        observations: Iterable[dict[str, Any]],
        states: Mapping[str, Any],
        accepted_reviews: Iterable[dict[str, Any]],
        ) -> dict[str, Any]:
    """Build a deterministic, JSON-serializable bitemporal manifest.

    ``states`` maps official uncompressed canonical state SHA-256 digests to
    the corresponding complete state objects.  Bodies are verified while
    building but are not copied into the result.
    """
    normal_observations, valid_states = _normalise_observations(
        observations, states)
    by_act: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normal_observations:
        by_act[row["act_id"]].append(row)
    reviews, boundary_reviews = _normalise_reviews(
        accepted_reviews, by_act, valid_states)

    acts: dict[str, dict[str, Any]] = {}
    for act_id in sorted(by_act):
        act_observations = by_act[act_id]
        intervals = _materialise_act_intervals(
            act_id, act_observations, boundary_reviews)
        current_intervals = [row for row in intervals
                             if row["knowledge_to"] is None]
        reviewed_starts = [row["effective_from"] for row in current_intervals
                           if row["effective_from"] is not None]
        act_reviews = [row for row in reviews if row["act_id"] == act_id]
        acts[act_id] = {
            "act_id": act_id,
            "jurabk": act_observations[0]["jurabk"],
            "observations": copy.deepcopy(act_observations),
            "accepted_transitions": copy.deepcopy(act_reviews),
            "intervals": intervals,
            "norm_intervals": _norm_intervals(intervals, valid_states),
            "gaps": _gaps_for_act(
                act_id, act_observations, boundary_reviews),
            "coverage": {
                "observed_from": act_observations[0]["observed_at"],
                "observed_through": act_observations[-1]["observed_at"],
                "verified_legal_from": min(reviewed_starts)
                    if reviewed_starts else None,
                "exact_state_count": len({row["state_sha256"]
                                          for row in act_observations}),
                "accepted_transition_count": len(act_reviews),
                "has_unreviewed_transitions": any(
                    gap["kind"] == "unreviewed_state_transition"
                    for gap in _gaps_for_act(
                        act_id, act_observations, boundary_reviews)),
            },
        }

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "date_semantics": {
            "effective_from": "inclusive legal-valid date; accepted review only",
            "effective_to": "exclusive legal-valid date; accepted review only",
            "knowledge_from": "inclusive date this assertion became knowable",
            "knowledge_to": "exclusive supersession date; null means current",
            "observed_at": "GII retrieval date; never an effective date",
            "verified_through_observed_at":
                "latest exact observation backing an open legal interval",
        },
        "as_of_observed_at": max(
            (row["observed_at"] for row in normal_observations), default=None),
        "state_catalog": {
            digest: {
                "act_id": state["id"],
                "jurabk": state["jurabk"],
                "norm_count": state["norm_count"],
                "state_sha256": digest,
            }
            for digest, state in sorted(valid_states.items())
        },
        "acts": acts,
    }
    # Enforce the public promise that the materialized object is plain JSON.
    canonical_json_bytes(result)
    return result


def _validate_manifest_header(history: Any) -> dict[str, Any]:
    if not isinstance(history, dict) or \
            history.get("schema_version") != SCHEMA_VERSION or \
            history.get("kind") != KIND or \
            not isinstance(history.get("acts"), dict):
        raise RetrospectiveHistoryError(
            "unsupported retrospective history manifest")
    return history


def _act(history: dict[str, Any], act_id: str) -> dict[str, Any]:
    _validate_manifest_header(history)
    if not isinstance(act_id, str) or act_id not in history["acts"]:
        raise RetrospectiveHistoryError(f"unknown act: {act_id}")
    act = history["acts"][act_id]
    if not isinstance(act, dict) or not isinstance(act.get("intervals"), list):
        raise RetrospectiveHistoryError("act history has an invalid shape")
    return act


def _interval_covers_knowledge(row: dict[str, Any], known_at: str) -> bool:
    start = _iso_day(row.get("knowledge_from"), "knowledge_from")
    end = _iso_day(row.get("knowledge_to"), "knowledge_to", nullable=True)
    if end is not None and start >= end:
        raise RetrospectiveHistoryError("invalid knowledge interval")
    return start <= known_at and (end is None or known_at < end)


def _interval_covers_legal(row: dict[str, Any], legal_at: str) -> bool:
    start = _iso_day(row.get("effective_from"), "effective_from", nullable=True)
    end = _iso_day(row.get("effective_to"), "effective_to", nullable=True)
    through = _iso_day(row.get("verified_through_observed_at"),
                       "verified_through_observed_at")
    if start is None:
        return False
    if end is not None and start >= end:
        raise RetrospectiveHistoryError("invalid legal interval")
    if legal_at < start or (end is not None and legal_at >= end):
        return False
    # An accepted outgoing review closes the legal interval exactly.  An open
    # end is safe only through the most recent exact state observation.
    return end is not None or legal_at <= through


def checkout_at(
        history: dict[str, Any], states: Mapping[str, Any], *,
        act_id: str, legal_at: str, known_at: str | None = None,
        norm: str | None = None, occurrence: int | None = None,
        ) -> dict[str, Any]:
    """Return one exact state at legal time as known at transaction time.

    Unknown legal starts, unreviewed boundaries, observation-only future
    ranges, duplicate norm labels without ``occurrence``, and overlapping
    assertions all raise :class:`RetrospectiveHistoryError`.
    """
    act = _act(history, act_id)
    legal_at = _iso_day(legal_at, "legal_at")
    if known_at is None:
        known_at = history.get("as_of_observed_at")
    known_at = _iso_day(known_at, "known_at")
    candidates = []
    for row in act["intervals"]:
        if not isinstance(row, dict):
            raise RetrospectiveHistoryError("act interval must be an object")
        if _interval_covers_knowledge(row, known_at) and \
                _interval_covers_legal(row, legal_at):
            candidates.append(row)
    if not candidates:
        raise RetrospectiveHistoryError(
            f"no verified legal state for {act_id} at {legal_at} "
            f"as known on {known_at}")
    if len(candidates) != 1:
        raise RetrospectiveHistoryError(
            f"ambiguous legal state for {act_id} at {legal_at}")
    interval = candidates[0]
    digest = _valid_digest(interval.get("state_sha256"), "state_sha256")
    if digest not in states:
        raise RetrospectiveHistoryError(
            f"checkout state is missing from CAS: {digest}")
    state = _validate_state(digest, states[digest])
    if state["id"] != act_id:
        raise RetrospectiveHistoryError(
            "checkout state belongs to another act")

    selected: Any = state
    if norm is not None:
        if not isinstance(norm, str) or not norm:
            raise RetrospectiveHistoryError("norm must be a non-empty label")
        matches = [(index, row) for index, row in enumerate(state["norms"])
                   if row["enbez"] == norm]
        if not matches:
            raise RetrospectiveHistoryError(
                f"norm {norm!r} is absent from checked-out state")
        if occurrence is None:
            if len(matches) != 1:
                raise RetrospectiveHistoryError(
                    f"norm {norm!r} is duplicated; occurrence is required")
            occurrence = 0
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or \
                occurrence < 0 or occurrence >= len(matches):
            raise RetrospectiveHistoryError("invalid norm occurrence")
        norm_index, norm_row = matches[occurrence]
        selected = {
            "enbez": norm,
            "occurrence": occurrence,
            "norm_index": norm_index,
            "norm_sha256": _digest(norm_row),
            "body": copy.deepcopy(norm_row),
        }
    return {
        "act_id": act_id,
        "legal_at": legal_at,
        "known_at": known_at,
        "state_sha256": digest,
        "interval": copy.deepcopy(interval),
        "value": copy.deepcopy(selected),
    }


def _indexed_norms(state: dict[str, Any]) -> dict[tuple[str, int], dict]:
    occurrences: dict[str, int] = defaultdict(int)
    indexed: dict[tuple[str, int], dict] = {}
    for norm in state["norms"]:
        key = (norm["enbez"], occurrences[norm["enbez"]])
        occurrences[norm["enbez"]] += 1
        indexed[key] = norm
    return indexed


def diff_between(
        history: dict[str, Any], states: Mapping[str, Any], *,
        act_id: str, from_date: str, to_date: str,
        known_at: str | None = None, norm: str | None = None,
        occurrence: int | None = None,
        ) -> dict[str, Any]:
    """Diff exact legal checkouts at two dates using one knowledge horizon."""
    from_date = _iso_day(from_date, "from_date")
    to_date = _iso_day(to_date, "to_date")
    if from_date > to_date:
        raise RetrospectiveHistoryError(
            "from_date must not be after to_date")
    before = checkout_at(
        history, states, act_id=act_id, legal_at=from_date,
        known_at=known_at)
    after = checkout_at(
        history, states, act_id=act_id, legal_at=to_date,
        known_at=before["known_at"])
    old_state = before["value"]
    new_state = after["value"]
    old_norms = _indexed_norms(old_state)
    new_norms = _indexed_norms(new_state)
    keys = list(old_norms)
    keys.extend(key for key in new_norms if key not in old_norms)
    if norm is not None:
        if not isinstance(norm, str) or not norm:
            raise RetrospectiveHistoryError("norm must be a non-empty label")
        keys = [key for key in keys if key[0] == norm and
                (occurrence is None or key[1] == occurrence)]
        if occurrence is None and len({key[1] for key in keys}) > 1:
            raise RetrospectiveHistoryError(
                f"norm {norm!r} is duplicated; occurrence is required")
        if not keys:
            raise RetrospectiveHistoryError(
                f"norm {norm!r} is absent from both states")

    changes: list[dict[str, Any]] = []
    for enbez, number in keys:
        old = old_norms.get((enbez, number))
        new = new_norms.get((enbez, number))
        if old == new:
            continue
        changes.append({
            "enbez": enbez,
            "occurrence": number,
            "operation": ("add" if old is None else
                          "delete" if new is None else "replace"),
            "old_norm_sha256": _digest(old) if old is not None else None,
            "new_norm_sha256": _digest(new) if new is not None else None,
            "old": copy.deepcopy(old),
            "new": copy.deepcopy(new),
        })
    metadata_fields = ("jurabk", "title", "stand", "build", "norm_count")
    metadata_changes = {
        field: {"old": old_state.get(field), "new": new_state.get(field)}
        for field in metadata_fields
        if old_state.get(field) != new_state.get(field)
    }
    return {
        "act_id": act_id,
        "known_at": before["known_at"],
        "from": {
            "legal_at": from_date,
            "state_sha256": before["state_sha256"],
            "version_id": before["interval"]["version_id"],
        },
        "to": {
            "legal_at": to_date,
            "state_sha256": after["state_sha256"],
            "version_id": after["interval"]["version_id"],
        },
        "metadata_changes": metadata_changes,
        "changes": changes,
    }


def _rfc3339(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrospectiveHistoryError(
            "built_at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise RetrospectiveHistoryError(
            "built_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _assertion_id(prefix: str, row: Mapping[str, Any],
                  volatile_fields: Iterable[str] = ()) -> str:
    excluded = {"id", "knowledge_from", "knowledge_to",
                *volatile_fields}
    payload = {key: value for key, value in row.items()
               if key not in excluded}
    return f"{prefix}:{_sha256(canonical_json_bytes(payload))[:24]}"


def _merge_assertions(current: list[dict[str, Any]], previous: Iterable[dict],
                      built_at: str, prefix: str, *,
                      volatile_fields: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Append corrections without rewriting the prior knowledge timeline."""
    volatile_fields = tuple(volatile_fields)
    previous_rows = list(previous)
    if any(not isinstance(row, dict) for row in previous_rows):
        raise RetrospectiveHistoryError(
            "previous assertions must be objects")
    prior = [_json_copy(row) for row in previous_rows]
    prior_ids: set[str] = set()
    for row in prior:
        assertion_id = str(row.get("id") or "")
        if not assertion_id:
            raise RetrospectiveHistoryError(
                "previous assertion has no stable id")
        if assertion_id in prior_ids:
            raise RetrospectiveHistoryError(
                f"duplicate previous assertion id: {assertion_id}")
        prior_ids.add(assertion_id)
        knowledge_from = _rfc3339(str(row.get("knowledge_from") or ""))
        knowledge_to = row.get("knowledge_to")
        if knowledge_to is not None:
            knowledge_to = _rfc3339(str(knowledge_to))
            if knowledge_to <= knowledge_from or knowledge_to > built_at:
                raise RetrospectiveHistoryError(
                    "previous assertion has an invalid knowledge interval")
        if knowledge_from > built_at:
            raise RetrospectiveHistoryError(
                "previous assertion starts after the current build")
    closed = [row for row in prior if row.get("knowledge_to") is not None]
    open_rows = {str(row["id"]): row for row in prior
                 if row.get("knowledge_to") is None}
    merged = list(closed)
    current_rows: dict[str, dict[str, Any]] = {}
    for raw in current:
        row = _json_copy(raw)
        row["id"] = _assertion_id(
            prefix, row, volatile_fields=volatile_fields)
        assertion_id = row["id"]
        duplicate = current_rows.get(assertion_id)
        if duplicate is not None:
            excluded = {"knowledge_from", "knowledge_to", *volatile_fields}
            left = {key: value for key, value in duplicate.items()
                    if key not in excluded}
            right = {key: value for key, value in row.items()
                     if key not in excluded}
            if canonical_json_bytes(left) != canonical_json_bytes(right):
                raise RetrospectiveHistoryError(
                    f"assertion id collision: {assertion_id}")
            if canonical_json_bytes(row) < canonical_json_bytes(duplicate):
                current_rows[assertion_id] = row
            continue
        current_rows[assertion_id] = row

    seen: set[str] = set(current_rows)
    for assertion_id, row in sorted(current_rows.items()):
        old = open_rows.get(assertion_id)
        if old is not None:
            preserved = copy.deepcopy(old)
            preserved["knowledge_to"] = None
            merged.append(preserved)
        else:
            if assertion_id in prior_ids:
                raise RetrospectiveHistoryError(
                    "a closed assertion cannot be silently reactivated")
            row["knowledge_from"] = built_at
            row["knowledge_to"] = None
            merged.append(row)
    for assertion_id, old in open_rows.items():
        if assertion_id in seen:
            continue
        old_from = _rfc3339(str(old.get("knowledge_from") or ""))
        if built_at <= old_from:
            raise RetrospectiveHistoryError(
                "a correction must be built after the assertion it closes")
        old["knowledge_to"] = built_at
        merged.append(old)
    return sorted(merged, key=lambda row: (
        str(row.get("knowledge_from") or ""),
        str(row.get("effective_from") or row.get("effective_at") or
            row.get("published_at") or ""),
        str(row.get("id") or "")))


def _review_evidence(provenance: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    observation = provenance.get("observation")
    if isinstance(observation, dict):
        evidence.append({
            "source": "GII",
            "url": observation.get("source_url"),
            "observed_at": observation.get("last_observed_at"),
            "state_sha256": observation.get("state_sha256"),
        })
    for key in ("incoming_legal_review", "outgoing_legal_review"):
        review = provenance.get(key)
        if not isinstance(review, dict):
            continue
        for row in review.get("evidence") or []:
            if isinstance(row, dict):
                evidence.append(copy.deepcopy(row))
    unique = {canonical_json_bytes(row): row for row in evidence}
    return [unique[key] for key in sorted(unique)]


def _public_interval(row: Mapping[str, Any]) -> dict[str, Any] | None:
    effective_from = row.get("effective_from")
    if effective_from is None:
        return None
    published = row.get("published_at")
    provenance = row.get("provenance") \
        if isinstance(row.get("provenance"), dict) else {}
    return {
        "segment_id": row.get("segment_id"),
        "version_id": row.get("version_id"),
        "act_id": row.get("act_id"),
        "state_sha256": row.get("state_sha256"),
        "previous_state_sha256": row.get("previous_state_sha256"),
        "effective_from": effective_from,
        "effective_to": row.get("effective_to"),
        "published_at": published,
        "observed_at": row.get("observed_at"),
        "last_observed_at": row.get("last_observed_at"),
        "verified_through_observed_at": row.get(
            "verified_through_observed_at"),
        "text_status": "official_exact",
        "date_status": "official_verified",
        "date_basis": row.get("date_basis"),
        "verification": row.get("verification"),
        "confidence": row.get("confidence"),
        "retroactive": bool(published and effective_from < published),
        "gaps": [],
        "evidence": _review_evidence(provenance),
        "provenance": copy.deepcopy(provenance),
    }


def _public_event(candidate: Mapping[str, Any]) -> dict[str, Any]:
    published = _iso_day(
        candidate.get("publication_date"), "publication_date")
    effective = _iso_day(
        candidate.get("effective_at"), "effective_at", nullable=True)
    generated = str(candidate.get("generated_at") or "")
    ingested = _rfc3339(generated) if generated else None
    observed = ingested[:10] if ingested else None
    pdf_sha = _valid_digest(candidate.get("pdf_sha256"), "pdf_sha256")
    if candidate.get("candidate_only") is not True or \
            candidate.get("historical_text_reconstructed") is not False:
        raise RetrospectiveHistoryError(
            "BGBl inventory row must remain candidate-only")
    for field in ("official_html_url", "official_pdf_url"):
        url = str(candidate.get(field) or "")
        if not url.startswith("https://www.recht.bund.de/"):
            raise RetrospectiveHistoryError(
                f"BGBl inventory has invalid {field}")
    gaps = []
    if effective is None:
        gaps.append({
            "kind": "effective_date_unresolved",
            "reason": candidate.get("effective_date_status"),
            "scope": "amending_article",
        })
    event = {
        "event_id": candidate.get("id"),
        "act_id": candidate.get("act_id"),
        "jurabk": candidate.get("jurabk"),
        # Display/navigation date only.  The two source dates below remain
        # authoritative and separate; clients must not reverse this projection.
        "date": effective or published,
        "published_at": published,
        "effective_at": effective,
        "observed_at": observed,
        "ingested_at": ingested,
        "retroactive": bool(effective and effective < published),
        "date_status": ("official_verified" if effective else "unknown"),
        "date_basis": ("official_dip_article_commencement_clause"
                       if effective else str(candidate.get(
                           "effective_date_status") or "unknown")),
        "text_status": "event_only",
        "verification": "exact_name_in_integrity_checked_final_bgbl_article",
        "candidate_only": True,
        "historical_text_reconstructed": False,
        "document_id": candidate.get("document_id"),
        "procedure_id": candidate.get("procedure_id"),
        "procedure_title": candidate.get("procedure_title"),
        "amending_article": candidate.get("amending_article"),
        "article_heading": candidate.get("article_heading"),
        "command_scope_status": candidate.get("command_scope_status"),
        "collective_subsection": candidate.get("collective_subsection"),
        "affected_norms": list(candidate.get("affected_norms") or []),
        "commands": copy.deepcopy(candidate.get("commands") or []),
        "command_count": int(candidate.get("command_count") or 0),
        "effective_date_status": candidate.get("effective_date_status"),
        "official_html_url": candidate.get("official_html_url"),
        "official_pdf_url": candidate.get("official_pdf_url"),
        "pdf_sha256": pdf_sha,
        "text_sha256": candidate.get("text_sha256"),
        "article_text_sha256": candidate.get("article_text_sha256"),
        "gaps": gaps,
        "evidence": [{
            "source": "BGBl",
            "url": candidate.get("official_pdf_url"),
            "document": candidate.get("document_id"),
            "sha256": candidate.get("pdf_sha256"),
        }, {
            "source": "DIP",
            "url": ("https://dip.bundestag.de/vorgang/" +
                    str(candidate.get("procedure_id") or "")),
            "procedure": candidate.get("procedure_id"),
        }],
    }
    return event


def build_public_manifest(
        history: Mapping[str, Any], states: Mapping[str, Any],
        objects: Mapping[str, Any], candidates: Iterable[dict[str, Any]], *,
        built_at: str, previous: Mapping[str, Any] | None = None,
        verified_reconstructions: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
    """Materialize the API/HF manifest with RFC3339 knowledge assertions.

    The strict internal history proves state/review chains.  This public layer
    adds the independently captured 2023+ BGBl event inventory and transaction
    timestamps.  A rerun preserves unchanged ``knowledge_from`` values and
    closes replaced assertions at the new ``built_at`` timestamp.
    """
    _validate_manifest_header(history)
    built_at = _rfc3339(built_at)
    if previous is not None and (
            not isinstance(previous, Mapping) or
            previous.get("schema_version") != SCHEMA_VERSION or
            previous.get("kind") != PUBLIC_KIND or
            not isinstance(previous.get("acts"), Mapping)):
        raise RetrospectiveHistoryError(
            "previous public history has an unsupported schema")
    if previous is not None:
        previous_built_at = _rfc3339(str(previous.get("built_at") or ""))
        if built_at < previous_built_at:
            raise RetrospectiveHistoryError(
                "built_at precedes the previous public history")
    previous_acts = (previous.get("acts") or {}
                     if previous is not None else {})
    if not isinstance(objects, Mapping):
        raise RetrospectiveHistoryError("public CAS objects must be a mapping")
    public_objects = copy.deepcopy(dict(
        previous.get("objects") or {} if previous is not None else {}))
    for digest, metadata in objects.items():
        _valid_digest(digest, "public object SHA-256")
        if not isinstance(metadata, Mapping):
            raise RetrospectiveHistoryError(
                f"public object metadata is invalid for {digest}")
        prior = public_objects.get(digest)
        if prior is not None and prior != metadata:
            raise RetrospectiveHistoryError(
                f"public object metadata changed for {digest}")
        public_objects[digest] = copy.deepcopy(dict(metadata))

    reconstructed_by_act: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if verified_reconstructions is not None:
        if not isinstance(verified_reconstructions, Mapping) or \
                verified_reconstructions.get("schema_version") != 1 or \
                verified_reconstructions.get("kind") != \
                "lexgraph-reviewed-verified-reconstructions":
            raise RetrospectiveHistoryError(
                "verified reconstruction artifact has an unsupported schema")
        recon_objects = verified_reconstructions.get("object_metadata") or {}
        if not isinstance(recon_objects, Mapping):
            raise RetrospectiveHistoryError(
                "verified reconstruction object metadata is invalid")
        for digest, metadata in recon_objects.items():
            _valid_digest(digest, "derived state SHA-256")
            if not isinstance(metadata, Mapping):
                raise RetrospectiveHistoryError(
                    "derived state object metadata is invalid")
            prior = public_objects.get(digest)
            if prior is not None and prior != metadata:
                raise RetrospectiveHistoryError(
                    f"derived object metadata changed for {digest}")
            public_objects[digest] = copy.deepcopy(dict(metadata))
        for row in verified_reconstructions.get("reconstructions") or []:
            if not isinstance(row, Mapping):
                raise RetrospectiveHistoryError(
                    "verified reconstruction row is invalid")
            act_id = str(row.get("act_id") or "")
            digest = _valid_digest(row.get("state_sha256"),
                                   "derived state SHA-256")
            anchor = _valid_digest(row.get("anchor_state_sha256"),
                                   "derived anchor SHA-256")
            if digest not in public_objects or anchor not in public_objects or \
                    digest not in states or anchor not in states:
                raise RetrospectiveHistoryError(
                    f"{act_id} reconstruction references an absent CAS state")
            for state_digest in (digest, anchor):
                state = _validate_state(state_digest, states[state_digest])
                if state.get("id") != act_id or \
                        state.get("jurabk") != row.get("jurabk"):
                    raise RetrospectiveHistoryError(
                        f"{act_id} reconstruction state identity mismatch")
            if row.get("text_status") != "derived_verified" or \
                    row.get("body_complete") is not True or \
                    row.get("source_exact") is not False or \
                    row.get("reverse_replay_verified") is not True:
                raise RetrospectiveHistoryError(
                    f"{act_id} reconstruction crossed its evidence boundary")
            common = {
                "act_id": act_id,
                "knowledge_from": row.get("knowledge_from"),
                "knowledge_to": row.get("knowledge_to"),
                "date_status": row.get("date_status"),
                "date_basis": row.get("date_basis"),
                "verification": row.get("verification"),
                "confidence": "verified_replay",
                "observed_at": row.get("observed_at"),
                "verified_through_observed_at": row.get("observed_at"),
                "retroactive": bool(row.get("retroactive")),
                "gaps": copy.deepcopy(row.get("gaps") or []),
                "evidence": copy.deepcopy(row.get("evidence") or []),
                "body_complete": True,
                "reverse_replay_verified": True,
            }
            reconstructed_by_act[act_id].append({
                **common,
                "segment_id": row.get("id"),
                "state_sha256": digest,
                "previous_state_sha256": None,
                "effective_from": row.get("effective_from"),
                "effective_to": row.get("effective_to"),
                "published_at": row.get("published_at"),
                "text_status": "derived_verified",
                "source_exact": False,
                "provenance": {
                    "method": "reviewed_inverse_then_canonical_forward_replay",
                    "anchor_state_sha256": anchor,
                    "incoming_event": copy.deepcopy(row.get("incoming_event")),
                    "outgoing_event": copy.deepcopy(row.get("outgoing_event")),
                    "changes_reversed": copy.deepcopy(
                        row.get("changes_reversed") or []),
                },
            })
            outgoing = row.get("outgoing_event") or {}
            reconstructed_by_act[act_id].append({
                **common,
                "segment_id": f"{row.get('id')}:anchor",
                "state_sha256": anchor,
                "previous_state_sha256": digest,
                "effective_from": row.get("effective_to"),
                "effective_to": None,
                "published_at": outgoing.get("published_at"),
                "text_status": "official_exact",
                "source_exact": True,
                "retroactive": bool(
                    outgoing.get("published_at") and row.get("effective_to")
                    and row["effective_to"] < outgoing["published_at"]),
                "provenance": {
                    "method": "official_anchor_with_verified_forward_boundary",
                    "anchor_state_sha256": anchor,
                    "outgoing_event": copy.deepcopy(outgoing),
                },
            })
    by_act_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("act_id"):
            raise RetrospectiveHistoryError(
                "BGBl candidate must be an act-linked object")
        by_act_events[str(candidate["act_id"])].append(
            _public_event(candidate))

    public_acts: dict[str, dict[str, Any]] = {}
    for act_id, internal in sorted(history["acts"].items()):
        current_intervals = []
        for row in internal.get("intervals") or []:
            if row.get("knowledge_to") is None:
                public = _public_interval(row)
                if public:
                    current_intervals.append(public)
        current_intervals.extend(reconstructed_by_act.get(act_id, []))
        previous_act = previous_acts.get(act_id) \
            if isinstance(previous_acts.get(act_id), dict) else {}
        intervals = _merge_assertions(
            current_intervals, previous_act.get("intervals") or [],
            built_at, "retro-interval")
        events = _merge_assertions(
            by_act_events.get(act_id, []), previous_act.get("events") or [],
            built_at, "retro-event",
            volatile_fields=("observed_at", "ingested_at"))
        observation_digests = {
            str(row.get("state_sha256") or "")
            for row in internal.get("observations") or []}
        if not observation_digests <= set(public_objects) or \
                not observation_digests <= set(states):
            raise RetrospectiveHistoryError(
                f"{act_id} references state objects absent from public CAS")
        ordered_observations = sorted(
            (row for row in internal.get("observations") or []
             if isinstance(row, dict)),
            key=lambda row: (
                str(row.get("observed_at") or ""),
                str(row.get("builddate") or ""),
                str(row.get("state_sha256") or "")))
        for observation in ordered_observations:
            digest = str(observation.get("state_sha256") or "")
            state_row = _validate_state(digest, states[digest])
            if state_row["id"] != act_id or \
                    state_row["jurabk"] != internal.get("jurabk"):
                raise RetrospectiveHistoryError(
                    f"{act_id} public state metadata mismatch")
        state = (states[str(ordered_observations[-1]["state_sha256"])]
                 if ordered_observations else None)
        legal_dates = [str(row.get("effective_from")) for row in intervals
                       if row.get("knowledge_to") is None and
                       row.get("effective_from")]
        event_dates = [str(row.get("effective_at") or row.get("published_at"))
                       for row in events if row.get("knowledge_to") is None and
                       (row.get("effective_at") or row.get("published_at"))]
        public_acts[act_id] = {
            "act_id": act_id,
            "jurabk": internal.get("jurabk"),
            "title": state.get("title") if isinstance(state, dict) else None,
            "history_start": min(legal_dates + event_dates)
                if legal_dates or event_dates else None,
            "intervals": intervals,
            "events": events,
            "observations": copy.deepcopy(internal.get("observations") or []),
            "gaps": copy.deepcopy(internal.get("gaps") or []),
            "coverage": copy.deepcopy(internal.get("coverage") or {}),
        }
    # Event-only acts cannot occur for a correctly joined curated corpus, but
    # fail closed instead of silently dropping one if source identifiers drift.
    unknown = set(by_act_events) - set(public_acts)
    if unknown:
        raise RetrospectiveHistoryError(
            f"BGBl candidates reference unknown acts: {sorted(unknown)}")

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": PUBLIC_KIND,
        "built_at": built_at,
        "state_identity": "sha256-canonical-uncompressed-json",
        "date_semantics": {
            "effective": "[effective_from,effective_to)",
            "knowledge": "[knowledge_from,knowledge_to)",
            "legal_dates": "YYYY-MM-DD",
            "knowledge_timestamps": "RFC3339 UTC",
            "published_at": "official BGBl publication date",
            "observed_at": "retrieval date; never substituted for legal time",
        },
        "source_policy": {
            "official_only": True,
            "effective_dates_inferred": False,
            "buzer_input": False,
            "event_gate": "exact act name in integrity-checked final BGBl article",
            "state_gate": (
                "either a complete official GII state pair with final BGBl/DIP "
                "review, or a body-complete reviewed inverse replay whose "
                "canonical forward replay exactly reproduces an official GII "
                "anchor; derived bytes remain source_exact=false"
            ),
        },
        "objects": public_objects,
        "acts": public_acts,
    }
    result["counts"] = {
        "acts": len(public_acts),
        "interval_assertions": sum(len(row["intervals"])
                                   for row in public_acts.values()),
        "current_intervals": sum(
            interval.get("knowledge_to") is None
            for row in public_acts.values() for interval in row["intervals"]),
        "events": sum(len(row["events"]) for row in public_acts.values()),
        "current_events": sum(
            event.get("knowledge_to") is None
            for row in public_acts.values() for event in row["events"]),
        "events_with_effective_date": sum(
            bool(event.get("effective_at"))
            for row in public_acts.values() for event in row["events"]
            if event.get("knowledge_to") is None),
        "observations": sum(len(row["observations"])
                            for row in public_acts.values()),
        "state_objects": len(public_objects),
    }
    canonical_json_bytes(result)
    return result


def write_sqlite(path: Path, manifest: Mapping[str, Any]) -> None:
    """Atomically build the portable retrospective SQLite database."""
    if manifest.get("schema_version") != SCHEMA_VERSION or \
            manifest.get("kind") != PUBLIC_KIND or \
            not isinstance(manifest.get("objects"), Mapping) or \
            not isinstance(manifest.get("acts"), Mapping):
        raise RetrospectiveHistoryError("unsupported public history manifest")
    _rfc3339(str(manifest.get("built_at") or ""))
    canonical_json_bytes(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    connection: sqlite3.Connection | None = None

    def encoded(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)

    try:
        connection = sqlite3.connect(temporary)
        connection.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA foreign_keys=ON;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE state_objects (
                state_sha256 TEXT PRIMARY KEY, path TEXT,
                canonical_bytes INTEGER, gzip_bytes INTEGER,
                gzip_sha256 TEXT, metadata_json TEXT NOT NULL
            );
            CREATE TABLE acts (
                act_id TEXT PRIMARY KEY, jurabk TEXT NOT NULL,
                title TEXT, history_start TEXT, coverage_json TEXT NOT NULL,
                gaps_json TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE state_observations (
                act_id TEXT NOT NULL REFERENCES acts(act_id),
                observed_at TEXT NOT NULL, state_sha256 TEXT NOT NULL
                    REFERENCES state_objects(state_sha256),
                builddate TEXT, source_url TEXT, verification TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (act_id, observed_at, state_sha256)
            );
            CREATE TABLE legal_intervals (
                id TEXT PRIMARY KEY, act_id TEXT NOT NULL REFERENCES acts(act_id),
                effective_from TEXT NOT NULL, effective_to TEXT,
                knowledge_from TEXT NOT NULL, knowledge_to TEXT,
                published_at TEXT, observed_at TEXT,
                verified_through_observed_at TEXT,
                state_sha256 TEXT NOT NULL
                    REFERENCES state_objects(state_sha256),
                previous_state_sha256 TEXT
                    REFERENCES state_objects(state_sha256),
                text_status TEXT NOT NULL, date_status TEXT NOT NULL,
                date_basis TEXT, verification TEXT, confidence TEXT,
                retroactive INTEGER NOT NULL, gaps_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE amendment_events (
                id TEXT PRIMARY KEY, event_id TEXT, act_id TEXT NOT NULL
                    REFERENCES acts(act_id),
                date TEXT NOT NULL, published_at TEXT NOT NULL,
                effective_at TEXT,
                observed_at TEXT, knowledge_from TEXT NOT NULL,
                knowledge_to TEXT, document_id TEXT, procedure_id TEXT,
                amending_article TEXT, article_heading TEXT,
                text_status TEXT NOT NULL, date_status TEXT NOT NULL,
                date_basis TEXT, retroactive INTEGER NOT NULL,
                affected_norms_json TEXT NOT NULL,
                commands_json TEXT NOT NULL, gaps_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX legal_at_idx ON legal_intervals
                (act_id, effective_from, effective_to);
            CREATE INDEX legal_knowledge_idx ON legal_intervals
                (act_id, knowledge_from, knowledge_to);
            CREATE INDEX event_effective_idx ON amendment_events
                (act_id, effective_at, published_at);
            CREATE INDEX event_date_idx ON amendment_events (act_id, date);
        """)
        for key in ("schema_version", "kind", "built_at", "state_identity"):
            connection.execute("INSERT INTO metadata VALUES (?, ?)", (
                key, encoded(manifest.get(key))))
        for key in ("date_semantics", "source_policy", "counts"):
            connection.execute("INSERT INTO metadata VALUES (?, ?)", (
                key, encoded(manifest.get(key))))
        for digest, metadata in sorted(
                (manifest.get("objects") or {}).items()):
            _valid_digest(digest, "SQLite state object SHA-256")
            if not isinstance(metadata, Mapping):
                raise RetrospectiveHistoryError(
                    f"invalid SQLite state object metadata for {digest}")
            connection.execute(
                "INSERT INTO state_objects VALUES (?,?,?,?,?,?)", (
                    digest, metadata.get("path"),
                    metadata.get("canonical_bytes"),
                    metadata.get("gzip_bytes"),
                    metadata.get("gzip_sha256"), encoded(metadata)))
        assertion_ids: set[str] = set()
        for act_id, act in sorted((manifest.get("acts") or {}).items()):
            if not isinstance(act, Mapping) or act.get("act_id") != act_id:
                raise RetrospectiveHistoryError(
                    f"invalid SQLite act row: {act_id}")
            connection.execute("INSERT INTO acts VALUES (?,?,?,?,?,?,?)", (
                act_id, act.get("jurabk"), act.get("title"),
                act.get("history_start"),
                encoded(act.get("coverage") or {}),
                encoded(act.get("gaps") or []), encoded(act)))
            observations = sorted(
                (row for row in act.get("observations") or []
                 if isinstance(row, Mapping)),
                key=canonical_json_bytes)
            for row in observations:
                connection.execute(
                    "INSERT INTO state_observations VALUES (?,?,?,?,?,?,?)", (
                        act_id, row.get("observed_at"),
                        row.get("state_sha256"), row.get("builddate"),
                        row.get("source_url"), row.get("verification"),
                        encoded(row)))
            intervals = sorted(
                (row for row in act.get("intervals") or []
                 if isinstance(row, Mapping)),
                key=canonical_json_bytes)
            for row in intervals:
                assertion_id = str(row.get("id") or "")
                if not assertion_id or assertion_id in assertion_ids:
                    raise RetrospectiveHistoryError(
                        f"invalid or duplicate SQLite assertion id: "
                        f"{assertion_id!r}")
                assertion_ids.add(assertion_id)
                connection.execute(
                    "INSERT INTO legal_intervals VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        assertion_id, act_id, row.get("effective_from"),
                        row.get("effective_to"), row.get("knowledge_from"),
                        row.get("knowledge_to"), row.get("published_at"),
                        row.get("observed_at"),
                        row.get("verified_through_observed_at"),
                        row.get("state_sha256"),
                        row.get("previous_state_sha256"),
                        row.get("text_status"), row.get("date_status"),
                        row.get("date_basis"), row.get("verification"),
                        row.get("confidence"), int(bool(row.get("retroactive"))),
                        encoded(row.get("gaps") or []),
                        encoded(row.get("provenance") or {}),
                        encoded(row.get("evidence") or []), encoded(row)))
            events = sorted(
                (row for row in act.get("events") or []
                 if isinstance(row, Mapping)),
                key=canonical_json_bytes)
            for row in events:
                assertion_id = str(row.get("id") or "")
                if not assertion_id or assertion_id in assertion_ids:
                    raise RetrospectiveHistoryError(
                        f"invalid or duplicate SQLite assertion id: "
                        f"{assertion_id!r}")
                assertion_ids.add(assertion_id)
                connection.execute(
                    "INSERT INTO amendment_events VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        assertion_id, row.get("event_id"), act_id,
                        row.get("date"), row.get("published_at"),
                        row.get("effective_at"),
                        row.get("observed_at"), row.get("knowledge_from"),
                        row.get("knowledge_to"), row.get("document_id"),
                        row.get("procedure_id"), row.get("amending_article"),
                        row.get("article_heading"), row.get("text_status"),
                        row.get("date_status"), row.get("date_basis"),
                        int(bool(row.get("retroactive"))),
                        encoded(row.get("affected_norms") or []),
                        encoded(row.get("commands") or []),
                        encoded(row.get("gaps") or []),
                        encoded(row.get("evidence") or []), encoded(row)))
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        connection = None
        os.replace(temporary, path)
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


__all__ = [
    "KIND",
    "PUBLIC_KIND",
    "OBSERVATION_DATE_BASIS",
    "OBSERVATION_VERIFICATION",
    "REVIEW_DATE_BASIS",
    "REVIEW_VERIFICATION",
    "RetrospectiveHistoryError",
    "canonical_json_bytes",
    "build_public_manifest",
    "checkout_at",
    "diff_between",
    "materialize_history",
    "write_sqlite",
]
