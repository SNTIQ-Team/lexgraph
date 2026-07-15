import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from federal_history import (  # noqa: E402
    build_public_federal_history,
    current_text_correspondence_events,
    exact_gii_state_events,
    validate_public_event,
)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")


def _patch(**updates):
    row = {
        "patch_id": "patch:dip:42:a1.n1",
        "target_act": "DemoG",
        "ref": {"para": "1"},
        "operation": "replace",
        "new_text": ("die überprüfte neue Formulierung mit genügend "
                     "eindeutigem Kontext für den Abgleich"),
        "status": "published",
        "source_doc": "bt-ds:21/42",
        "procedure": "dip-vorgang:42",
        "procedure_title": "Demo-Änderungsgesetz",
        "published_at": "2026-07-01",
        "valid_from": "2026-07-02",
    }
    row.update(updates)
    return row


def test_patch_requires_current_official_text_match():
    norms = [{"jurabk": "DemoG", "enbez": "§ 1",
              "slug": "demog",
              "text": ("Hier steht die überprüfte neue Formulierung mit "
                       "genügend eindeutigem Kontext für den Abgleich.")}]
    events = current_text_correspondence_events(
        [_patch()], norms, "2026-07-15")
    assert len(events) == 1
    assert events[0]["verification"] == "current_text_correspondence"
    assert events[0]["effective_at"] is None
    assert events[0]["published_at"] is None
    assert events[0]["procedure_status_at"] == "2026-07-01"
    assert events[0]["draft_bill_declared_effective_at"] == "2026-07-02"
    assert events[0]["historical_attribution"] is False
    assert events[0]["verification_scope"] == \
        "current_text_correspondence_only"
    assert events[0]["evidence"][0]["url"].endswith("/42")

    assert current_text_correspondence_events(
        [_patch(new_text="steht nicht im Gesetz")], norms, "2026-07-15") == []
    assert current_text_correspondence_events(
        [_patch(status="proposed")], norms, "2026-07-15") == []
    assert current_text_correspondence_events(
        [_patch(new_text="zu kurzer Treffer")], norms, "2026-07-15") == []
    assert current_text_correspondence_events(
        [_patch(old_text_constraint="Hier steht")], norms,
        "2026-07-15") == []

    duplicate = norms[0] | {"text": norms[0]["text"] * 2}
    assert current_text_correspondence_events(
        [_patch()], [duplicate], "2026-07-15") == []


def test_exact_state_pair_is_hashed_and_new_acts_are_not_fake_changes(tmp_path):
    old, new = tmp_path / "2026-07-14", tmp_path / "2026-07-15"
    _write_jsonl(old / "acts.jsonl", [
        {"jurabk": "DemoG", "slug": "demog", "builddate": "20260701000000", "norm_count": 1},
    ])
    _write_jsonl(new / "acts.jsonl", [
        {"jurabk": "DemoG", "slug": "demog", "builddate": "20260702000000", "norm_count": 1},
        {"jurabk": "NewG", "slug": "newg", "builddate": "20260702000000", "norm_count": 1},
    ])
    _write_jsonl(old / "norms.jsonl", [
        {"jurabk": "DemoG", "enbez": "§ 1", "text": "alt", "doknr": "old"},
    ])
    _write_jsonl(new / "norms.jsonl", [
        {"jurabk": "DemoG", "enbez": "§ 1", "text": "neu", "doknr": "new"},
        {"jurabk": "NewG", "enbez": "§ 1", "text": "neu im Korpus"},
    ])
    events = exact_gii_state_events([new, old])
    assert len(events) == 1
    event = events[0]
    assert event["act"] == "DemoG"
    assert event["verification"] == "exact"
    assert event["effective_at"] is None
    assert event["date_basis"] == "retrieval_observation_not_effective_date"
    assert len(event["changes"][0]["old_sha256"]) == 64
    assert len(event["changes"][0]["new_sha256"]) == 64
    assert event["changes"][0]["old_present"] is True
    assert event["changes"][0]["new_present"] is True


def test_public_validator_rejects_private_candidates():
    with pytest.raises(ValueError, match="non-public"):
        validate_public_event({
            "verification": "candidate_private",
            "evidence": [{"source": "GII",
                          "url": "https://www.gesetze-im-internet.de/"}],
        })


def test_public_validator_rejects_fake_hosts_and_tampered_hashes():
    with pytest.raises(ValueError, match="official host"):
        validate_public_event({
            "verification": "metadata_only",
            "evidence": [{"source": "GII", "url": "https://example.com"}],
        })
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_public_event({
            "verification": "exact",
            "evidence": [{"source": "GII",
                          "url": "https://www.gesetze-im-internet.de/demog/"}],
            "changes": [{"old": "alt", "new": "neu",
                         "old_present": True, "new_present": True,
                         "old_sha256": "0" * 64,
                         "new_sha256": "1" * 64}],
        })


def test_public_build_policy_does_not_name_buzer_as_an_input(tmp_path):
    day = tmp_path / "2026-07-15"
    _write_jsonl(day / "norms.jsonl", [
        {"jurabk": "DemoG", "enbez": "§ 1",
         "slug": "demog",
         "text": ("die überprüfte neue Formulierung mit genügend "
                  "eindeutigem Kontext für den Abgleich")},
    ])
    result = build_public_federal_history(
        [_patch()],
        [{"jurabk": "DemoG", "enbez": "§ 1",
          "slug": "demog",
          "text": ("die überprüfte neue Formulierung mit genügend "
                   "eindeutigem Kontext für den Abgleich")}],
        [day], "2026-07-15")
    assert result["total"] == 1
    assert result["source_policy"] == {
        "official_only": True,
        "buzer_role": "private_candidate_and_cross_check",
        "effective_dates_inferred": False,
    }
