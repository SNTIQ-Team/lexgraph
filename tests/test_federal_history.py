import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from federal_history import (  # noqa: E402
    _sha256,
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


def _complete_pair_event(changes):
    """A complete GII state-pair event, as official_states produces them."""
    return {
        "verification": "exact",
        "complete_parsed_state_pair": True,
        "old_state_sha256": "a" * 64,
        "new_state_sha256": "b" * 64,
        "evidence": [{"source": "GII",
                      "url": "https://www.gesetze-im-internet.de/bgb/"}],
        "changes": changes,
    }


def _change(**updates):
    text = "Auf den Tausch finden die Vorschriften über den Kauf Anwendung."
    row = {
        "para": "§ 480", "old": text, "new": text,
        "old_present": True, "new_present": True,
        "old_sha256": _sha256(text), "new_sha256": _sha256(text),
        "operation": "replace",
        "old_title": "Tausch", "new_title": "Tausch",
        "old_glied": "Untertitel 4 Tausch", "new_glied": "Untertitel 5 Tausch",
        "old_norm_sha256": "c" * 64, "new_norm_sha256": "d" * 64,
    }
    row.update(updates)
    return row


def test_outline_only_change_with_identical_body_text_is_public():
    """GII renumbered BGB § 480 from Untertitel 4 to 5 without touching § 480's
    body.  That is a real, provable state change, not a fabricated one."""
    validate_public_event(_complete_pair_event([_change()]))


def test_removal_of_an_empty_bodied_norm_is_public():
    """GewSchG § 1c is a heading-only norm: deleting it leaves both text sides
    empty, so presence — not text — is the proof of change."""
    validate_public_event(_complete_pair_event([_change(
        para="§ 1c", operation="delete", old="", new="",
        old_present=True, new_present=False,
        old_sha256=_sha256(""), new_sha256=_sha256(""),
        old_title="", new_title=None,
        old_glied="", new_glied=None,
        old_norm_sha256="c" * 64, new_norm_sha256=None,
    )]))


def test_identical_norm_states_are_still_rejected():
    with pytest.raises(ValueError, match="distinct"):
        validate_public_event(_complete_pair_event([_change(
            old_norm_sha256="c" * 64, new_norm_sha256="c" * 64)]))
    with pytest.raises(ValueError, match="distinct"):
        validate_public_event(_complete_pair_event([_change(
            old_glied="Untertitel 4 Tausch", new_glied="Untertitel 4 Tausch",
            old_norm_sha256=None, new_norm_sha256=None)]))
    with pytest.raises(ValueError, match="distinct"):
        validate_public_event(_complete_pair_event([_change(
            old_present=False, new_present=False)]))
