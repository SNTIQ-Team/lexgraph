from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from retrospective_history import (  # noqa: E402
    OBSERVATION_DATE_BASIS,
    OBSERVATION_VERIFICATION,
    REVIEW_DATE_BASIS,
    REVIEW_VERIFICATION,
    RetrospectiveHistoryError,
    build_public_manifest,
    canonical_json_bytes,
    checkout_at,
    diff_between,
    materialize_history,
    write_sqlite,
)
from api.retrospective_store import (  # noqa: E402
    RetrospectiveNotFound,
    resolve_interval as resolve_public_interval,
    validate_manifest as validate_public_manifest,
)


def _sha(value: bytes | str) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _digest(value: object) -> str:
    return _sha(canonical_json_bytes(value))


def _state(text: str, *, title: str = "Zweck") -> dict:
    return {
        "id": "fed_demog",
        "jurabk": "DemoG",
        "juris": "DE",
        "title": "Demonstrationsgesetz",
        "stand": "Stand: Test",
        "build": "20260101",
        "norm_count": 1,
        "norms": [{
            "enbez": "§ 1", "titel": title, "text": text,
            "glied": "Teil 1",
        }],
    }


def _observation(day: str, digest: str, *, build: str = "20260101") -> dict:
    return {
        "act_id": "fed_demog",
        "jurabk": "DemoG",
        "observed_at": day,
        "state_sha256": digest,
        "builddate": build,
        "norm_count": 1,
        "source_url": "https://www.gesetze-im-internet.de/demog/",
        "date_basis": OBSERVATION_DATE_BASIS,
        "verification": OBSERVATION_VERIFICATION,
    }


def _change(old: dict, new: dict) -> dict:
    before = old["norms"][0]
    after = new["norms"][0]
    return {
        "para": "§ 1",
        "old": before["text"],
        "new": after["text"],
        "old_present": True,
        "new_present": True,
        "old_sha256": _sha(before["text"]),
        "new_sha256": _sha(after["text"]),
        "operation": "replace",
        "old_title": before["titel"],
        "new_title": after["titel"],
        "old_glied": before["glied"],
        "new_glied": after["glied"],
        "old_norm_sha256": _digest(before),
        "new_norm_sha256": _digest(after),
    }


def _review(*, old: dict, new: dict, old_digest: str, new_digest: str,
            previous_observed: str, observed: str, published: str,
            effective: str, suffix: str) -> dict:
    pdf_sha = _sha(f"pdf-{suffix}")
    return {
        "id": f"fed-review:{suffix}",
        "schema_version": 1,
        "act_id": "fed_demog",
        "act": "DemoG",
        "jurabk": "DemoG",
        "published_at": published,
        "effective_at": effective,
        "observed_at": observed,
        "previous_observed_at": previous_observed,
        "date_basis": REVIEW_DATE_BASIS,
        "verification": REVIEW_VERIFICATION,
        "state_sha256": new_digest,
        "previous_state_sha256": old_digest,
        "changes": [_change(old, new)],
        "procedure_id": suffix,
        "bgbl": {
            "document_id": f"bgbl-{suffix}",
            "pdf_sha256": pdf_sha,
            "integrity_verified": True,
        },
        "evidence": [
            {"source": "GII", "state_sha256": old_digest},
            {"source": "BGBl", "sha256": pdf_sha},
            {"source": "DIP", "procedure": suffix},
            {"source": "GII", "state_sha256": new_digest},
        ],
        "derivation": {
            "tool": "test",
            "effective_dates_inferred": False,
        },
    }


def _objects(states: dict[str, dict]) -> dict[str, dict]:
    return {
        digest: {
            "path": f"objects/sha256/{digest[:2]}/{digest}.json.gz",
            "canonical_bytes": len(canonical_json_bytes(state)),
            "gzip_bytes": 123,
            "gzip_sha256": _sha(f"gzip-{digest}"),
        }
        for digest, state in states.items()
    }


def _candidate(*, generated_at: str = "2026-07-16T09:00:00Z") -> dict:
    return {
        "id": "fed-bgbl-candidate:demo",
        "candidate_only": True,
        "historical_text_reconstructed": False,
        "act_id": "fed_demog",
        "jurabk": "DemoG",
        "publication_date": "2026-02-28",
        "effective_at": "2026-03-01",
        "generated_at": generated_at,
        "effective_date_status": "resolved_remainder_clause",
        "document_id": "bgbl-1-2026-1",
        "procedure_id": "123",
        "procedure_title": "Änderungsgesetz",
        "amending_article": "2",
        "article_heading": "Änderung des DemoG",
        "affected_norms": ["§ 1"],
        "commands": [{"operation": "replace", "norm": "§ 1"}],
        "command_count": 1,
        "official_html_url": "https://www.recht.bund.de/example.html",
        "official_pdf_url": "https://www.recht.bund.de/example.pdf",
        "pdf_sha256": "d" * 64,
        "text_sha256": "e" * 64,
        "article_text_sha256": "f" * 64,
    }


@pytest.fixture
def evidence() -> dict:
    old, middle, new = _state("alt"), _state("mitte"), _state("neu")
    old_sha, middle_sha, new_sha = map(
        _digest, (old, middle, new))
    states = {old_sha: old, middle_sha: middle, new_sha: new}
    observations = [
        _observation("2026-01-10", old_sha),
        _observation("2026-02-10", middle_sha, build="20260201"),
        _observation("2026-02-15", middle_sha, build="20260201"),
        _observation("2026-03-10", new_sha, build="20260301"),
    ]
    reviews = [
        _review(
            old=old, new=middle, old_digest=old_sha,
            new_digest=middle_sha, previous_observed="2026-01-10",
            observed="2026-02-10", published="2026-01-31",
            effective="2026-02-01", suffix="one"),
        _review(
            old=middle, new=new, old_digest=middle_sha,
            new_digest=new_sha, previous_observed="2026-02-15",
            observed="2026-03-10", published="2026-02-28",
            effective="2026-03-01", suffix="two"),
    ]
    return {
        "states": states,
        "observations": observations,
        "reviews": reviews,
        "digests": (old_sha, middle_sha, new_sha),
    }


def test_materializes_json_manifest_and_two_independent_date_axes(
        evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    assert json.loads(canonical_json_bytes(history)) == history
    assert history["as_of_observed_at"] == "2026-03-10"
    act = history["acts"]["fed_demog"]
    assert act["coverage"] == {
        "observed_from": "2026-01-10",
        "observed_through": "2026-03-10",
        "verified_legal_from": "2026-02-01",
        "exact_state_count": 3,
        "accepted_transition_count": 2,
        "has_unreviewed_transitions": False,
    }
    assert [gap["kind"] for gap in act["gaps"]] == [
        "unknown_effective_start"]

    old_sha, middle_sha, new_sha = evidence["digests"]
    current = [row for row in act["intervals"]
               if row["knowledge_to"] is None]
    old_row = next(row for row in current
                   if row["state_sha256"] == old_sha)
    middle_row = next(row for row in current
                      if row["state_sha256"] == middle_sha)
    new_row = next(row for row in current
                   if row["state_sha256"] == new_sha)
    assert old_row["effective_from"] is None
    assert old_row["effective_to"] == "2026-02-01"
    assert middle_row["published_at"] == "2026-01-31"
    assert middle_row["effective_from"] == "2026-02-01"
    assert middle_row["effective_to"] == "2026-03-01"
    assert middle_row["observed_at"] == "2026-02-10"
    assert middle_row["last_observed_at"] == "2026-02-15"
    assert new_row["effective_from"] == "2026-03-01"
    assert new_row["effective_to"] is None
    assert new_row["observed_at"] == "2026-03-10"
    assert all(row["text_status"] == "official_exact_complete_state"
               for row in current)
    assert len(act["norm_intervals"]) == len(act["intervals"])
    assert all("text" not in row for row in act["norm_intervals"])


def test_bitemporal_checkout_does_not_leak_later_review(evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    middle_sha = evidence["digests"][1]

    early_knowledge = checkout_at(
        history, evidence["states"], act_id="fed_demog",
        legal_at="2026-02-05", known_at="2026-02-10")
    assert early_knowledge["state_sha256"] == middle_sha
    assert early_knowledge["interval"]["effective_to"] is None
    assert early_knowledge["value"]["norms"][0]["text"] == "mitte"

    with pytest.raises(RetrospectiveHistoryError,
                       match="no verified legal state"):
        checkout_at(
            history, evidence["states"], act_id="fed_demog",
            legal_at="2026-02-20", known_at="2026-02-15")

    later_knowledge = checkout_at(
        history, evidence["states"], act_id="fed_demog",
        legal_at="2026-02-20", known_at="2026-03-10",
        norm="§ 1")
    assert later_knowledge["state_sha256"] == middle_sha
    assert later_knowledge["interval"]["effective_to"] == "2026-03-01"
    assert later_knowledge["value"]["body"]["text"] == "mitte"


def test_observed_at_is_never_used_as_effective_date(evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], [])
    act = history["acts"]["fed_demog"]
    assert all(row["effective_from"] is None
               for row in act["intervals"])
    assert act["coverage"]["verified_legal_from"] is None
    assert act["coverage"]["has_unreviewed_transitions"] is True
    assert [gap["kind"] for gap in act["gaps"]].count(
        "unreviewed_state_transition") == 2
    with pytest.raises(RetrospectiveHistoryError,
                       match="no verified legal state"):
        checkout_at(
            history, evidence["states"], act_id="fed_demog",
            legal_at="2026-03-10")


def test_unreviewed_later_state_closes_coverage_not_legal_date(
        evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"],
        evidence["reviews"][:1])
    with pytest.raises(RetrospectiveHistoryError,
                       match="no verified legal state"):
        checkout_at(
            history, evidence["states"], act_id="fed_demog",
            legal_at="2026-02-20", known_at="2026-03-10")
    result = checkout_at(
        history, evidence["states"], act_id="fed_demog",
        legal_at="2026-02-15", known_at="2026-03-10")
    assert result["state_sha256"] == evidence["digests"][1]
    assert result["interval"]["effective_to"] is None
    assert result["interval"]["verified_through_observed_at"] == \
        "2026-02-15"


def test_exact_diff_between_verified_legal_dates(evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    result = diff_between(
        history, evidence["states"], act_id="fed_demog",
        from_date="2026-02-20", to_date="2026-03-01",
        known_at="2026-03-10", norm="§ 1")
    assert result["from"]["state_sha256"] == evidence["digests"][1]
    assert result["to"]["state_sha256"] == evidence["digests"][2]
    assert result["changes"] == [{
        "enbez": "§ 1",
        "occurrence": 0,
        "operation": "replace",
        "old_norm_sha256": _digest(evidence["states"][
            evidence["digests"][1]]["norms"][0]),
        "new_norm_sha256": _digest(evidence["states"][
            evidence["digests"][2]]["norms"][0]),
        "old": evidence["states"][evidence["digests"][1]]["norms"][0],
        "new": evidence["states"][evidence["digests"][2]]["norms"][0],
    }]


@pytest.mark.parametrize("mutation,match", [
    (lambda review: review.update(previous_observed_at="2026-01-09"),
     "not an adjacent observed state pair"),
    (lambda review: review["changes"][0].update(new="fabricated"),
     "changes do not match complete state pair"),
    (lambda review: review["bgbl"].update(integrity_verified=False),
     "lacks verified GII/BGBl/DIP evidence"),
])
def test_rejects_invalid_or_ambiguous_review_evidence(
        evidence: dict, mutation, match: str) -> None:
    review = json.loads(json.dumps(evidence["reviews"][0]))
    mutation(review)
    with pytest.raises(RetrospectiveHistoryError, match=match):
        materialize_history(
            evidence["observations"], evidence["states"], [review])


def test_preserves_official_retroactive_review(evidence: dict) -> None:
    review = json.loads(json.dumps(evidence["reviews"][0]))
    review["published_at"] = "2026-02-02"
    history = materialize_history(
        evidence["observations"], evidence["states"], [review])
    accepted = history["acts"]["fed_demog"]["accepted_transitions"][0]
    assert accepted["effective_at"] == "2026-02-01"
    assert accepted["published_at"] == "2026-02-02"
    assert accepted["retroactive"] is True


def test_rejects_multiple_reviews_for_one_boundary(evidence: dict) -> None:
    first = evidence["reviews"][0]
    conflicting = json.loads(json.dumps(first))
    conflicting["id"] = "fed-review:conflict"
    with pytest.raises(RetrospectiveHistoryError,
                       match="multiple accepted reviews"):
        materialize_history(
            evidence["observations"], evidence["states"],
            [first, conflicting])


def test_rejects_duplicate_review_id_and_buzer_evidence(evidence: dict) -> None:
    first = evidence["reviews"][0]
    duplicate_id = json.loads(json.dumps(first))
    duplicate_id["effective_at"] = "2026-02-02"
    with pytest.raises(RetrospectiveHistoryError,
                       match="duplicate accepted review id"):
        materialize_history(
            evidence["observations"], evidence["states"],
            [first, duplicate_id])

    contaminated = json.loads(json.dumps(first))
    contaminated["evidence"].append({
        "source": "Buzer", "url": "https://www.buzer.de/"})
    with pytest.raises(RetrospectiveHistoryError,
                       match="lacks verified GII/BGBl/DIP evidence"):
        materialize_history(
            evidence["observations"], evidence["states"], [contaminated])


def test_rejects_corrupt_state_and_same_day_ambiguity(evidence: dict) -> None:
    bad_states = dict(evidence["states"])
    bad_states[evidence["digests"][0]] = _state("tampered")
    with pytest.raises(RetrospectiveHistoryError, match="hash mismatch"):
        materialize_history(
            evidence["observations"], bad_states, evidence["reviews"])

    conflicting = dict(evidence["observations"][0])
    conflicting["builddate"] = "20260102"
    with pytest.raises(RetrospectiveHistoryError,
                       match="ambiguous observations"):
        materialize_history(
            [*evidence["observations"], conflicting],
            evidence["states"], evidence["reviews"])


def test_query_revalidates_intervals_and_cas(evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    corrupt = json.loads(json.dumps(history))
    middle_sha = evidence["digests"][1]
    row = next(row for row in corrupt["acts"]["fed_demog"]["intervals"]
               if row["state_sha256"] == middle_sha and
               row["knowledge_to"] is None)
    row["effective_to"] = row["effective_from"]
    with pytest.raises(RetrospectiveHistoryError,
                       match="invalid legal interval"):
        checkout_at(
            corrupt, evidence["states"], act_id="fed_demog",
            legal_at="2026-02-01", known_at="2026-03-10")

    bad_states = dict(evidence["states"])
    bad_states[middle_sha] = _state("tampered")
    with pytest.raises(RetrospectiveHistoryError, match="hash mismatch"):
        checkout_at(
            history, bad_states, act_id="fed_demog",
            legal_at="2026-02-20", known_at="2026-03-10")


def test_public_assertion_ids_are_stable_across_reruns_and_input_order(
        evidence: dict) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    objects = _objects(evidence["states"])
    earlier = _candidate(generated_at="2026-07-16T09:00:00Z")
    later = _candidate(generated_at="2026-07-16T10:00:00Z")
    first = build_public_manifest(
        history, evidence["states"], objects, [later, earlier],
        built_at="2026-07-16T12:00:00Z")
    validate_public_manifest(first)
    reordered = build_public_manifest(
        history, evidence["states"], objects, [earlier, later],
        built_at="2026-07-16T12:00:00Z")
    assert first == reordered

    rerun = build_public_manifest(
        history, evidence["states"], objects,
        [_candidate(generated_at="2026-07-17T10:00:00Z")],
        built_at="2026-07-17T12:00:00Z", previous=first)
    first_act = first["acts"]["fed_demog"]
    rerun_act = rerun["acts"]["fed_demog"]
    assert first_act["events"][0]["date"] == "2026-03-01"
    assert first_act["events"][0]["effective_at"] == "2026-03-01"
    assert first_act["events"][0]["published_at"] == "2026-02-28"
    assert rerun_act["intervals"] == first_act["intervals"]
    assert rerun_act["events"] == first_act["events"]
    assert all(row["knowledge_from"] == "2026-07-16T12:00:00Z"
               for row in rerun_act["intervals"] + rerun_act["events"])


def test_public_correction_closes_old_knowledge_interval(evidence: dict) -> None:
    objects = _objects(evidence["states"])
    initial_history = materialize_history(
        evidence["observations"], evidence["states"],
        evidence["reviews"][:1])
    initial = build_public_manifest(
        initial_history, evidence["states"], objects, [],
        built_at="2026-07-16T12:00:00Z")
    corrected_history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    corrected = build_public_manifest(
        corrected_history, evidence["states"], objects, [],
        built_at="2026-07-17T12:00:00Z", previous=initial)
    validated = validate_public_manifest(corrected)

    middle_sha = evidence["digests"][1]
    assertions = [row for row in corrected["acts"]["fed_demog"]["intervals"]
                  if row["state_sha256"] == middle_sha]
    assert len(assertions) == 2
    old = next(row for row in assertions
               if row["knowledge_to"] is not None)
    new = next(row for row in assertions
               if row["knowledge_to"] is None)
    assert old["effective_to"] is None
    assert old["knowledge_from"] == "2026-07-16T12:00:00Z"
    assert old["knowledge_to"] == "2026-07-17T12:00:00Z"
    assert new["effective_to"] == "2026-03-01"
    assert new["knowledge_from"] == "2026-07-17T12:00:00Z"
    assert old["id"] != new["id"]
    assert resolve_public_interval(
        validated, "fed_demog", "2026-02-15",
        as_of="2026-07-16T12:00:00Z")["state_sha256"] == middle_sha
    with pytest.raises(RetrospectiveNotFound):
        resolve_public_interval(
            validated, "fed_demog", "2026-02-20",
            as_of="2026-07-16T12:00:00Z")
    assert resolve_public_interval(
        validated, "fed_demog", "2026-02-20",
        as_of="2026-07-17T12:00:00Z")["state_sha256"] == middle_sha


def test_public_manifest_preserves_retroactive_effective_date(
        evidence: dict) -> None:
    review = json.loads(json.dumps(evidence["reviews"][0]))
    review["published_at"] = "2026-02-02"
    history = materialize_history(
        evidence["observations"], evidence["states"], [review])
    public = build_public_manifest(
        history, evidence["states"], _objects(evidence["states"]), [],
        built_at="2026-07-16T12:00:00Z")
    interval = public["acts"]["fed_demog"]["intervals"][0]
    assert interval["effective_from"] == "2026-02-01"
    assert interval["published_at"] == "2026-02-02"
    assert interval["retroactive"] is True
    assert validate_public_manifest(public)["acts"]["fed_demog"][
        "intervals"][0]["retroactive"] is True


def test_sqlite_is_deterministic_lossless_and_queryable(
        evidence: dict, tmp_path: Path) -> None:
    history = materialize_history(
        evidence["observations"], evidence["states"], evidence["reviews"])
    manifest = build_public_manifest(
        history, evidence["states"], _objects(evidence["states"]),
        [_candidate()], built_at="2026-07-16T12:00:00Z")
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    write_sqlite(first, manifest)
    write_sqlite(second, manifest)
    assert first.read_bytes() == second.read_bytes()

    with sqlite3.connect(first) as database:
        counts = {
            table: database.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "acts", "state_objects", "state_observations",
                "legal_intervals", "amendment_events")
        }
        assert counts == {
            "acts": manifest["counts"]["acts"],
            "state_objects": manifest["counts"]["state_objects"],
            "state_observations": manifest["counts"]["observations"],
            "legal_intervals": manifest["counts"]["interval_assertions"],
            "amendment_events": manifest["counts"]["events"],
        }
        row = database.execute("""
            SELECT state_sha256, payload_json
            FROM legal_intervals
            WHERE act_id = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR ? < effective_to)
              AND knowledge_from <= ?
              AND (knowledge_to IS NULL OR ? < knowledge_to)
              AND (effective_to IS NOT NULL
                   OR ? <= verified_through_observed_at)
        """, ("fed_demog", "2026-03-05", "2026-03-05",
              "2026-07-16T12:00:00Z", "2026-07-16T12:00:00Z",
              "2026-03-05")).fetchone()
        assert row is not None
        assert row[0] == evidence["digests"][2]
        assert json.loads(row[1])["state_sha256"] == row[0]
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
