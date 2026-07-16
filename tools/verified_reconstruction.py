#!/usr/bin/env python3
"""Fail-closed reconstruction of complete federal states from official evidence.

This module does not attempt to understand arbitrary German amendment law.
It executes only a deliberately small command grammar whose complete target,
old/new wording and structural position can be proved against a complete GII
anchor.  A reconstruction is accepted only when all of the following hold:

* the reviewed specification pins the GII anchor and every BGBl/DIP hash/date;
* every command in the official candidate is consumed exactly once;
* inverse operations have one target and one occurrence in the addressed norm;
* the reconstructed state's incoming amendment is present at the claimed
  legal boundary; and
* replaying the outgoing commands recreates the canonical anchor SHA-256.

The resulting body is therefore ``derived_verified`` rather than an official
historical GII snapshot.  Retrieval time, legal-validity time and Lexgraph's
knowledge time remain separate in the emitted artifact.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from official_states import (
    DEFAULT_STORE,
    canonical_json_bytes,
    load_manifest,
    load_state_verified,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEWS = ROOT / "data" / "verified_reconstruction_reviews.json"
REVIEW_KIND = "lexgraph-verified-reconstruction-reviews"
ARTIFACT_KIND = "lexgraph-reviewed-verified-reconstructions"
SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PARAGRAPH_MARKER = re.compile(r"(?<!\w)\((\d+[a-z]?)\)")
_SENTENCE_END = re.compile(r"[.!?](?=\s*(?:[A-ZÄÖÜ§(]|$))")


class ReconstructionError(ValueError):
    """Reviewed evidence is incomplete, ambiguous or fails replay."""


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ReconstructionError(f"{field} must be a SHA-256 digest")
    return value


def _require_day(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ReconstructionError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ReconstructionError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ReconstructionError(f"{field} must be YYYY-MM-DD")
    return value


def _require_instant(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ReconstructionError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconstructionError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ReconstructionError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        missing = sorted(required - set(value if isinstance(value, dict) else ()))
        extra = sorted(set(value if isinstance(value, dict) else ()) - required)
        raise ReconstructionError(
            f"{label} has unsupported fields (missing={missing}, extra={extra})")
    return value


def _validate_reviews(raw: Any) -> list[dict[str, Any]]:
    document = _exact_keys(
        raw, {"schema_version", "kind", "reviews"}, "review document")
    if document["schema_version"] != SCHEMA_VERSION or \
            document["kind"] != REVIEW_KIND:
        raise ReconstructionError("unsupported reconstruction review schema")
    if not isinstance(document["reviews"], list) or not document["reviews"]:
        raise ReconstructionError("review document has no reconstructions")
    seen: set[str] = set()
    reviews: list[dict[str, Any]] = []
    review_fields = {
        "id", "act_id", "jurabk", "anchor", "interval",
        "incoming", "outgoing",
    }
    anchor_fields = {
        "state_sha256", "observed_at", "builddate", "source_url",
        "state_build", "state_stand",
    }
    interval_fields = {"effective_from", "effective_to"}
    event_fields = {
        "candidate_id", "document_id", "procedure_id", "amending_article",
        "execution_date", "publication_date", "effective_at",
        "effective_date_status", "pdf_sha256", "pdf_md5", "text_sha256",
        "article_text_sha256", "expected_commands",
    }
    for index, raw_review in enumerate(document["reviews"]):
        review = _exact_keys(raw_review, review_fields, f"review {index}")
        review_id = review.get("id")
        if not isinstance(review_id, str) or not review_id or review_id in seen:
            raise ReconstructionError("review id is empty or duplicated")
        seen.add(review_id)
        for field in ("act_id", "jurabk"):
            if not isinstance(review.get(field), str) or not review[field]:
                raise ReconstructionError(f"{review_id} has invalid {field}")
        anchor = _exact_keys(
            review["anchor"], anchor_fields, f"{review_id} anchor")
        _require_digest(anchor["state_sha256"], "anchor state_sha256")
        _require_day(anchor["observed_at"], "anchor observed_at")
        for field in ("builddate", "source_url", "state_build", "state_stand"):
            if not isinstance(anchor.get(field), str) or not anchor[field]:
                raise ReconstructionError(
                    f"{review_id} anchor has invalid {field}")
        interval = _exact_keys(
            review["interval"], interval_fields, f"{review_id} interval")
        start = _require_day(interval["effective_from"], "effective_from")
        end = _require_day(interval["effective_to"], "effective_to")
        if start >= end:
            raise ReconstructionError(
                f"{review_id} interval is empty or reversed")
        for event_name in ("incoming", "outgoing"):
            event = _exact_keys(
                review[event_name], event_fields,
                f"{review_id} {event_name} event")
            for field in (
                    "candidate_id", "document_id", "procedure_id",
                    "amending_article", "execution_date", "publication_date",
                    "effective_at", "effective_date_status", "pdf_sha256",
                    "pdf_md5", "text_sha256", "article_text_sha256"):
                if not isinstance(event.get(field), str) or not event[field]:
                    raise ReconstructionError(
                        f"{review_id} {event_name} has invalid {field}")
            _require_day(event["execution_date"], "execution_date")
            _require_day(event["publication_date"], "publication_date")
            _require_day(event["effective_at"], "effective_at")
            for field in ("pdf_sha256", "text_sha256", "article_text_sha256"):
                _require_digest(event[field], field)
            if not re.fullmatch(r"[0-9a-f]{32}", event["pdf_md5"]):
                raise ReconstructionError("reviewed PDF MD5 is invalid")
            if not isinstance(event["expected_commands"], list) or \
                    not event["expected_commands"]:
                raise ReconstructionError(
                    f"{review_id} {event_name} has no commands")
        if review["incoming"]["effective_at"] != start or \
                review["outgoing"]["effective_at"] != end:
            raise ReconstructionError(
                f"{review_id} interval does not match event boundaries")
        if review["anchor"]["observed_at"] < end:
            raise ReconstructionError(
                f"{review_id} anchor predates the outgoing boundary")
        reviews.append(copy.deepcopy(review))
    return reviews


def _strict_command(raw_command: Any) -> dict[str, str]:
    if not isinstance(raw_command, dict):
        raise ReconstructionError("candidate command must be an object")
    raw = raw_command.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        raise ReconstructionError("candidate command has no raw text")
    # New inventories preserve every command and pin it independently.  Old
    # inventories had an 800-character UI cap; without a digest, reaching that
    # boundary remains a hard failure.  A verified digest permits genuinely
    # long official recasts instead of confusing length with truncation.
    raw_digest = raw_command.get("raw_sha256")
    if raw_digest is not None:
        _require_digest(raw_digest, "command raw_sha256")
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != raw_digest:
            raise ReconstructionError("candidate command raw SHA-256 mismatch")
    elif len(raw) >= 800 or "…" in raw:
        raise ReconstructionError("candidate command is truncated or partial")
    command = _compact(raw)
    quote_count = command.count("„")
    if quote_count != command.count("“"):
        raise ReconstructionError("candidate command has unbalanced quotes")

    def require_ref(result: Mapping[str, str], fields: tuple[str, ...]) -> None:
        ref = raw_command.get("ref")
        if not isinstance(ref, dict):
            raise ReconstructionError("candidate command has no target ref")
        expected_ref = {
            "para": result["norm"].removeprefix("§ "),
            **{field: result[field] for field in fields},
        }
        if any(str(ref.get(field) or "").replace(" ", "") != value
               for field, value in expected_ref.items()):
            raise ReconstructionError("candidate command ref disagrees with raw")

    address = r"§\s*(?P<norm>\d+[a-z]?)"
    replace = re.fullmatch(
        rf"In {address} Absatz (?P<absatz>\d+[a-z]?) Satz (?P<satz>\d+) "
        r"wird (?:die Angabe|das Wort|die Wörter|die Wortfolge|"
        r"die Bezeichnung) „(?P<old>[^„“]+)“ durch "
        r"(?:die Angabe|das Wort|die Wörter|die Wortfolge|"
        r"die Bezeichnung) „(?P<new>[^„“]+)“ ersetzt\.", command)
    if replace:
        if quote_count != 2 or raw_command.get("operation") != "replace":
            raise ReconstructionError("replacement command metadata disagrees")
        result = {
            "kind": "replace_literal",
            "norm": f"§ {replace.group('norm')}",
            "absatz": replace.group("absatz"),
            "satz": replace.group("satz"),
            "old": _compact(replace.group("old")),
            "new": _compact(replace.group("new")),
        }
        if _compact(raw_command.get("old_text_constraint")) != result["old"] or \
                _compact(raw_command.get("new_text")) != result["new"]:
            raise ReconstructionError("replacement old/new metadata disagrees")
        require_ref(result, ("absatz", "satz"))
        return result

    sentence = re.fullmatch(
        rf"In {address} Absatz (?P<absatz>\d+[a-z]?) wird nach Satz "
        r"(?P<after_sentence>\d+) der folgende Satz eingefügt: "
        r"„(?P<text>[^„“]+)“", command)
    if sentence:
        if quote_count != 1 or raw_command.get("operation") != "insert":
            raise ReconstructionError("sentence insertion metadata disagrees")
        result = {
            "kind": "insert_sentence_after",
            "norm": f"§ {sentence.group('norm')}",
            "absatz": sentence.group("absatz"),
            "after_sentence": sentence.group("after_sentence"),
            "text": _compact(sentence.group("text")),
        }
        if _compact(raw_command.get("new_text")) != result["text"]:
            raise ReconstructionError("sentence insertion text disagrees")
        require_ref(result, ("absatz",))
        if str((raw_command.get("ref") or {}).get("satz") or "") != \
                result["after_sentence"]:
            raise ReconstructionError("sentence insertion ref disagrees with raw")
        return result

    paragraph = re.fullmatch(
        rf"In {address} wird nach Absatz (?P<after_absatz>\d+[a-z]?) "
        r"der folgende Absatz (?P<new_absatz>\d+[a-z]?) eingefügt: "
        r"„(?P<text>[^„“]+)“", command)
    if paragraph:
        if quote_count != 1 or raw_command.get("operation") != "insert":
            raise ReconstructionError("paragraph insertion metadata disagrees")
        result = {
            "kind": "insert_paragraph_after",
            "norm": f"§ {paragraph.group('norm')}",
            "after_absatz": paragraph.group("after_absatz"),
            "new_absatz": paragraph.group("new_absatz"),
            "text": _compact(paragraph.group("text")),
        }
        if _compact(raw_command.get("new_text")) != result["text"]:
            raise ReconstructionError("paragraph insertion text disagrees")
        require_ref(result, ())
        if str((raw_command.get("ref") or {}).get("absatz") or "") != \
                result["after_absatz"]:
            raise ReconstructionError("paragraph insertion ref disagrees with raw")
        return result
    raise ReconstructionError(
        f"unsupported or non-leaf amendment command: {command[:120]}")


def _validated_candidate(candidate: Any, expected: Mapping[str, Any]
                         ) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(candidate, dict):
        raise ReconstructionError("reviewed candidate is not an object")
    exact_fields = (
        "id", "document_id", "procedure_id", "amending_article",
        "execution_date", "publication_date", "effective_at",
        "effective_date_status", "pdf_sha256", "text_sha256",
        "article_text_sha256",
    )
    mapping = {
        "candidate_id": "id",
        **{field: field for field in exact_fields if field != "id"},
    }
    for reviewed_field, candidate_field in mapping.items():
        if candidate.get(candidate_field) != expected.get(reviewed_field):
            raise ReconstructionError(
                f"candidate {candidate.get('id')} changed {candidate_field}")
    if candidate.get("pdf_md5") != expected.get("pdf_md5") or \
            candidate.get("advertised_md5") != expected.get("pdf_md5"):
        raise ReconstructionError("candidate PDF checksum metadata changed")
    if candidate.get("act_id") is None or candidate.get("jurabk") is None:
        raise ReconstructionError("candidate has no act identity")
    if candidate.get("candidate_only") is not True or \
            candidate.get("historical_text_reconstructed") is not False or \
            candidate.get("integrity_verified") is not True:
        raise ReconstructionError("candidate crossed its evidence boundary")
    if candidate.get("command_scope_status") != "whole_article" or \
            candidate.get("collective_subsection") is not None:
        raise ReconstructionError("candidate command scope is not whole-article")
    if not str(candidate.get("effective_date_status") or "").startswith(
            "resolved_"):
        raise ReconstructionError("candidate legal date is unresolved")
    for field, host in (
            ("official_html_url", "www.recht.bund.de"),
            ("official_pdf_url", "www.recht.bund.de")):
        parsed = urlparse(str(candidate.get(field) or ""))
        if parsed.scheme != "https" or parsed.hostname != host:
            raise ReconstructionError(f"candidate {field} is not official")
    commands = candidate.get("commands")
    if not isinstance(commands, list) or candidate.get("command_count") != len(commands):
        raise ReconstructionError("candidate command count is inconsistent")
    parsed_commands = [_strict_command(command) for command in commands]
    expected_commands = expected.get("expected_commands")
    if parsed_commands != expected_commands:
        raise ReconstructionError("candidate commands differ from review")
    affected = sorted(str(value) for value in candidate.get("affected_norms") or [])
    targeted = sorted({command["norm"] for command in parsed_commands})
    if affected != targeted:
        raise ReconstructionError("candidate affected norms are incomplete")
    return copy.deepcopy(candidate), parsed_commands


def _find_norm(state: dict[str, Any], label: str) -> dict[str, Any]:
    matches = [norm for norm in state.get("norms") or []
               if isinstance(norm, dict) and norm.get("enbez") == label]
    if len(matches) != 1:
        raise ReconstructionError(
            f"state has {len(matches)} occurrences of target norm {label}")
    return matches[0]


def _paragraphs(text: str) -> list[tuple[str, int, int, int]]:
    markers = list(_PARAGRAPH_MARKER.finditer(text))
    result = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        result.append((marker.group(1), marker.start(), marker.end(), end))
    return result


def _paragraph(text: str, number: str) -> tuple[int, int, int]:
    matches = [(start, body_start, end) for current, start, body_start, end
               in _paragraphs(text) if current == number]
    if len(matches) != 1:
        raise ReconstructionError(
            f"target paragraph {number} occurs {len(matches)} times")
    return matches[0]


def _sentence_number(body: str, offset: int) -> int:
    if offset < 0 or offset > len(body):
        raise ReconstructionError("sentence offset is outside paragraph")
    return sum(match.end() <= offset for match in _SENTENCE_END.finditer(body)) + 1


def _inverse_command(state: dict[str, Any], command: Mapping[str, str]) -> None:
    norm = _find_norm(state, command["norm"])
    text = norm["text"]
    if command["kind"] == "replace_literal":
        _start, body_start, end = _paragraph(text, command["absatz"])
        body = text[body_start:end]
        old, new = command["old"], command["new"]
        if text.count(new) != 1 or text.count(old) != 0 or body.count(new) != 1:
            raise ReconstructionError(
                f"replacement cardinality failed in {command['norm']}")
        if _sentence_number(body, body.index(new)) != int(command["satz"]):
            raise ReconstructionError(
                f"replacement is outside reviewed sentence in {command['norm']}")
        norm["text"] = text.replace(new, old, 1)
        return
    if command["kind"] == "insert_sentence_after":
        _start, body_start, end = _paragraph(text, command["absatz"])
        inserted = command["text"]
        body = text[body_start:end]
        if text.count(inserted) != 1 or body.count(inserted) != 1:
            raise ReconstructionError(
                f"inserted sentence cardinality failed in {command['norm']}")
        position = body.index(inserted)
        preceding_sentences = sum(
            match.end() <= position for match in _SENTENCE_END.finditer(body))
        if preceding_sentences != int(command["after_sentence"]):
            raise ReconstructionError(
                f"inserted sentence is outside reviewed position in {command['norm']}")
        norm["text"] = text.replace(inserted, "", 1)
        return
    raise ReconstructionError(
        f"command {command['kind']} is not reverse-executable")


def _forward_command(state: dict[str, Any], command: Mapping[str, str]) -> None:
    norm = _find_norm(state, command["norm"])
    text = norm["text"]
    if command["kind"] == "replace_literal":
        _start, body_start, end = _paragraph(text, command["absatz"])
        body = text[body_start:end]
        old, new = command["old"], command["new"]
        if text.count(old) != 1 or text.count(new) != 0 or body.count(old) != 1:
            raise ReconstructionError(
                f"forward replacement cardinality failed in {command['norm']}")
        if _sentence_number(body, body.index(old)) != int(command["satz"]):
            raise ReconstructionError(
                f"forward replacement has a different sentence target")
        norm["text"] = text.replace(old, new, 1)
        return
    if command["kind"] == "insert_sentence_after":
        start, body_start, end = _paragraph(text, command["absatz"])
        body = text[body_start:end]
        inserted = command["text"]
        if text.count(inserted) != 0:
            raise ReconstructionError("forward insertion already exists")
        sentence_ends = list(_SENTENCE_END.finditer(body))
        after = int(command["after_sentence"])
        if len(sentence_ends) != after:
            # Initial support deliberately allows insertion at the end of the
            # addressed paragraph only.  Inserting into a longer paragraph
            # requires a stronger structural AST and must not be guessed.
            raise ReconstructionError(
                f"paragraph has {len(sentence_ends)} sentences, expected {after}")
        # ``end`` is the next paragraph marker.  Any anchor-derived whitespace
        # before that marker remains before the newly inserted sentence, which
        # is exactly what inverse removal preserved.
        insertion_at = end
        norm["text"] = text[:insertion_at] + inserted + text[insertion_at:]
        return
    raise ReconstructionError(
        f"command {command['kind']} is not forward-executable")


def _assert_incoming(state: dict[str, Any], command: Mapping[str, str]) -> None:
    if command["kind"] != "insert_paragraph_after":
        raise ReconstructionError(
            f"unsupported incoming assertion {command['kind']}")
    norm = _find_norm(state, command["norm"])
    paragraphs = _paragraphs(norm["text"])
    labels = [row[0] for row in paragraphs]
    try:
        previous = labels.index(command["after_absatz"])
        inserted = labels.index(command["new_absatz"])
    except ValueError as exc:
        raise ReconstructionError("incoming paragraph is absent") from exc
    if inserted != previous + 1 or labels.count(command["new_absatz"]) != 1:
        raise ReconstructionError("incoming paragraph has ambiguous position")
    _number, start, _body_start, end = paragraphs[inserted]
    actual = _compact(norm["text"][start:end])
    if actual != command["text"]:
        raise ReconstructionError("incoming paragraph body differs from BGBl")


def _anchor_observation(
        manifest: Mapping[str, Any], review: Mapping[str, Any],
        states: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    anchor = review["anchor"]
    digest = anchor["state_sha256"]
    matches = [row for row in manifest.get("observations") or []
               if isinstance(row, dict)
               and row.get("act_id") == review["act_id"]
               and row.get("jurabk") == review["jurabk"]
               and row.get("state_sha256") == digest
               and row.get("observed_at") == anchor["observed_at"]]
    if len(matches) != 1:
        raise ReconstructionError("reviewed anchor observation is not unique")
    observation = copy.deepcopy(matches[0])
    for field in ("builddate", "source_url"):
        if observation.get(field) != anchor[field]:
            raise ReconstructionError(f"anchor observation changed {field}")
    if observation.get("date_basis") != \
            "retrieval_observation_not_effective_date" or \
            observation.get("verification") != "exact":
        raise ReconstructionError("anchor is not an exact GII observation")
    if digest not in states:
        raise ReconstructionError("anchor state object is missing")
    state = copy.deepcopy(states[digest])
    if _digest(state) != digest:
        raise ReconstructionError("anchor state object hash mismatch")
    if state.get("id") != review["act_id"] or \
            state.get("jurabk") != review["jurabk"]:
        raise ReconstructionError("anchor state identity differs from review")
    if state.get("build") != anchor["state_build"] or \
            state.get("stand") != anchor["state_stand"]:
        raise ReconstructionError("anchor projection metadata changed")
    return observation, state


def _event_summary(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["id"],
        "document_id": candidate["document_id"],
        "procedure_id": candidate["procedure_id"],
        "amending_article": candidate["amending_article"],
        "execution_date": candidate["execution_date"],
        "published_at": candidate["publication_date"],
        "effective_at": candidate["effective_at"],
        "effective_date_status": candidate["effective_date_status"],
        "article_text_sha256": candidate["article_text_sha256"],
        "official_html_url": candidate["official_html_url"],
        "official_pdf_url": candidate["official_pdf_url"],
        "pdf_sha256": candidate["pdf_sha256"],
        "text_sha256": candidate["text_sha256"],
    }


def build_reconstructions(
        review_document: Any, candidates: Iterable[dict[str, Any]],
        state_manifest: Mapping[str, Any], states: Mapping[str, Any], *,
        built_at: str) -> dict[str, Any]:
    """Build reviewed derived states entirely in memory."""
    reviews = _validate_reviews(review_document)
    built_at = _require_instant(built_at, "built_at")
    indexed: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
            raise ReconstructionError("candidate inventory has an invalid row")
        if candidate["id"] in indexed:
            raise ReconstructionError(f"duplicate candidate {candidate['id']}")
        indexed[candidate["id"]] = candidate

    reconstructions: list[dict[str, Any]] = []
    state_objects: dict[str, dict[str, Any]] = {}
    object_metadata: dict[str, dict[str, Any]] = {}
    for review in reviews:
        observation, anchor_state = _anchor_observation(
            state_manifest, review, states)
        try:
            incoming_raw = indexed[review["incoming"]["candidate_id"]]
            outgoing_raw = indexed[review["outgoing"]["candidate_id"]]
        except KeyError as exc:
            raise ReconstructionError(
                f"reviewed candidate is absent: {exc.args[0]}") from exc
        incoming, incoming_commands = _validated_candidate(
            incoming_raw, review["incoming"])
        outgoing, outgoing_commands = _validated_candidate(
            outgoing_raw, review["outgoing"])
        for candidate in (incoming, outgoing):
            if candidate["act_id"] != review["act_id"] or \
                    candidate["jurabk"] != review["jurabk"]:
                raise ReconstructionError("candidate belongs to another act")

        derived = copy.deepcopy(anchor_state)
        for command in reversed(outgoing_commands):
            _inverse_command(derived, command)
        for command in incoming_commands:
            _assert_incoming(derived, command)

        changed_norms = sorted({command["norm"] for command in outgoing_commands})
        actual_changed = sorted(
            old["enbez"] for old, new in zip(
                anchor_state["norms"], derived["norms"], strict=True)
            if old != new)
        if actual_changed != changed_norms:
            raise ReconstructionError(
                "inverse replay changed norms outside the reviewed command set")

        replayed = copy.deepcopy(derived)
        for command in outgoing_commands:
            _forward_command(replayed, command)
        anchor_digest = review["anchor"]["state_sha256"]
        replayed_digest = _digest(replayed)
        if replayed_digest != anchor_digest or replayed != anchor_state:
            raise ReconstructionError(
                "forward replay did not reproduce the canonical GII anchor")

        state_digest = _digest(derived)
        prior = state_objects.setdefault(state_digest, copy.deepcopy(derived))
        if prior != derived:
            raise ReconstructionError("derived state digest collision")
        canonical_bytes = len(canonical_json_bytes(derived))
        object_metadata[state_digest] = {
            "state_sha256": state_digest,
            "canonical_bytes": canonical_bytes,
            "origin": "derived_verified_reverse_replay",
            "anchor_state_sha256": anchor_digest,
            "source_exact": False,
        }
        incoming_summary = _event_summary(incoming)
        outgoing_summary = _event_summary(outgoing)
        evidence = [{
            "source": "GII",
            "url": observation["source_url"],
            "observed_at": observation["observed_at"],
            "state_sha256": anchor_digest,
        }, {
            "source": "BGBl",
            "url": incoming["official_pdf_url"],
            "document": incoming["document_id"],
            "pdf_sha256": incoming["pdf_sha256"],
            "text_sha256": incoming["text_sha256"],
        }, {
            "source": "DIP",
            "url": f"https://dip.bundestag.de/vorgang/{incoming['procedure_id']}",
            "procedure": incoming["procedure_id"],
        }]
        reconstructions.append({
            "id": review["id"],
            "schema_version": SCHEMA_VERSION,
            "act_id": review["act_id"],
            "jurabk": review["jurabk"],
            "state_sha256": state_digest,
            "anchor_state_sha256": anchor_digest,
            "text_status": "derived_verified",
            "body_complete": True,
            "source_exact": False,
            "reverse_replay_verified": True,
            "anchor_projection_metadata_retained": True,
            "date_status": "official_verified",
            "date_basis": "official_bgbl_dip_boundaries_and_verified_replay",
            "verification": "exact_cardinality_inverse_and_canonical_forward_replay",
            "effective_from": review["interval"]["effective_from"],
            "effective_to": review["interval"]["effective_to"],
            "knowledge_from": built_at,
            "knowledge_to": None,
            "published_at": incoming["publication_date"],
            "observed_at": observation["observed_at"],
            "retroactive": (
                review["interval"]["effective_from"] <
                incoming["publication_date"]),
            "incoming_event": incoming_summary,
            "outgoing_event": outgoing_summary,
            "changes_reversed": copy.deepcopy(outgoing_commands),
            "evidence": evidence,
            "gaps": [],
        })

    reconstructions.sort(key=lambda row: (
        row["act_id"], row["effective_from"], row["effective_to"], row["id"]))
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "kind": ARTIFACT_KIND,
        "built_at": built_at,
        "state_identity": "sha256-canonical-uncompressed-json",
        "reconstructions": reconstructions,
        "object_metadata": object_metadata,
        "state_objects": state_objects,
    }
    canonical_json_bytes(artifact)
    return artifact


def _latest_candidates() -> Path:
    root = ROOT / "data" / "snapshots" / "bgbl_history_backfill"
    paths = sorted(path / "candidates.jsonl" for path in root.iterdir()
                   if path.is_dir() and (path / "candidates.jsonl").is_file())
    if not paths:
        raise ReconstructionError("no BGBl retrospective candidate snapshot")
    return paths[-1]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconstructionError(f"cannot read {path}: {exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ReconstructionError(
                            f"{path}:{line_number} is not an object")
                    rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconstructionError(f"cannot read {path}: {exc}") from exc
    return rows


def _verified_source_objects(candidates: Iterable[dict[str, Any]]) -> None:
    """Re-hash the local official PDF/text CAS before reconstruction."""
    for candidate in candidates:
        for path_field, digest_field, expected_root in (
                ("pdf_object", "pdf_sha256", ROOT / "data" / "bgbl_documents"),
                ("text_object", "text_sha256", ROOT / "data" / "bgbl_documents")):
            raw_path = candidate.get(path_field)
            if not isinstance(raw_path, str) or not raw_path:
                raise ReconstructionError(f"candidate lacks {path_field}")
            path = (ROOT / raw_path).resolve()
            try:
                path.relative_to(expected_root.resolve())
            except ValueError as exc:
                raise ReconstructionError(
                    f"candidate {path_field} escapes official CAS") from exc
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise ReconstructionError(
                    f"cannot read candidate {path_field}: {path}") from exc
            if hashlib.sha256(payload).hexdigest() != candidate.get(digest_field):
                raise ReconstructionError(
                    f"candidate {path_field} SHA-256 mismatch")
            if path_field == "pdf_object":
                if hashlib.md5(payload).hexdigest() != candidate.get(  # noqa: S324
                        "advertised_md5"):
                    raise ReconstructionError("candidate PDF MD5 mismatch")


def build_from_paths(
        reviews_path: Path = DEFAULT_REVIEWS,
        candidates_path: Path | None = None,
        state_store: Path = DEFAULT_STORE, *, built_at: str) -> dict[str, Any]:
    review_document = _read_json(Path(reviews_path))
    candidates = _read_jsonl(Path(candidates_path or _latest_candidates()))
    reviewed_ids = {
        str(row.get("candidate_id"))
        for review in review_document.get("reviews") or []
        if isinstance(review, dict)
        for row in (review.get("incoming"), review.get("outgoing"))
        if isinstance(row, dict)
    }
    reviewed_candidates = [row for row in candidates if row.get("id") in reviewed_ids]
    if len(reviewed_candidates) != len(reviewed_ids):
        raise ReconstructionError("one or more reviewed candidates are absent")
    _verified_source_objects(reviewed_candidates)
    manifest = load_manifest(Path(state_store))
    anchors = {
        str(review.get("anchor", {}).get("state_sha256"))
        for review in review_document.get("reviews") or []
        if isinstance(review, dict)
    }
    states = {digest: load_state_verified(Path(state_store), digest)
              for digest in anchors}
    return build_reconstructions(
        review_document, candidates, manifest, states, built_at=built_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build fail-closed official derived reconstruction artifact")
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEWS)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--state-store", type=Path, default=DEFAULT_STORE)
    parser.add_argument(
        "--built-at", default=datetime.now(timezone.utc).isoformat(
            timespec="seconds"), help="RFC3339 Lexgraph knowledge timestamp")
    parser.add_argument("--output", type=Path,
                        help="write JSON here; default is stdout")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        artifact = build_from_paths(
            args.reviews, args.candidates, args.state_store,
            built_at=args.built_at)
    except ReconstructionError as exc:
        print(f"verified reconstruction failed: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(
        artifact, ensure_ascii=False, sort_keys=True,
        indent=None if args.compact else 2,
        separators=(",", ":") if args.compact else None) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
