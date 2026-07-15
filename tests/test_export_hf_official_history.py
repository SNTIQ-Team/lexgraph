from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from export_hf import _official_history_rows  # noqa: E402
from official_states import archive_gii_states, transitions  # noqa: E402


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")


def _snapshot(root: Path, day: str, text: str) -> Path:
    snapshot = root / day
    _jsonl(snapshot / "acts.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "norm_count": 1,
        "long_title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "builddate": day.replace("-", "") + "010101", "doknr": "A1",
    }])
    _jsonl(snapshot / "norms.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 1",
        "titel": "Zweck", "text": text, "gliederung": "Teil 1",
        "doknr": "N1",
    }])
    return snapshot


def _review_file(path: Path, transition: dict) -> None:
    review = {
        "id": "fed-review:test", "act_id": transition["act_id"],
        "jurabk": transition["jurabk"],
        "observed_at": transition["observed_at"],
        "previous_observed_at": transition["previous_observed_at"],
        "previous_state_sha256": transition["previous_state_sha256"],
        "state_sha256": transition["state_sha256"],
        "date_basis": "official_bgbl_command_and_commencement_clause",
        "verification": "official_final_text_and_complete_state_pair",
        "published_at": "2026-07-14", "effective_at": "2026-07-15",
        "changes": transition["changes"],
        "bgbl": {"integrity_verified": True, "pdf_sha256": "c" * 64},
        "evidence": [
            {"source": "GII",
             "url": "https://www.gesetze-im-internet.de/demog/"},
            {"source": "BGBl",
             "url": "https://www.recht.bund.de/example.pdf"},
            {"source": "DIP",
             "url": "https://dip.bundestag.de/vorgang/1"},
        ],
    }
    path.write_text(json.dumps({
        "schema_version": 1,
        "source_policy": {
            "official_only": True, "includes_quarantined_sources": False,
            "effective_dates_inferred": False,
        },
        "total": 1, "reviews": [review],
    }), encoding="utf-8")


def test_hf_history_materializes_verified_full_states_and_provenance(
        tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    old = _snapshot(snapshots, "2026-07-14", "alte Fassung")
    new = _snapshot(snapshots, "2026-07-15", "neue Fassung")
    manifest = archive_gii_states([old, new], store)
    transition = transitions(manifest, store)[0]
    reviews = tmp_path / "reviews.json"
    _review_file(reviews, transition)

    rows = _official_history_rows(store, reviews)
    assert len(rows["official_federal_state_observations.jsonl"]) == 2
    assert len(rows["official_federal_state_transitions.jsonl"]) == 1
    assert len(rows["official_transition_reviews.jsonl"]) == 1
    state_rows = list(rows["official_federal_state_objects.jsonl"])
    assert len(state_rows) == 2

    exported_transition = rows[
        "official_federal_state_transitions.jsonl"][0]
    assert exported_transition["effective_at"] is None
    assert exported_transition["provenance"] == {
        "source": "GII",
        "algorithm": "lexgraph-complete-state-diff",
        "date_basis": "retrieval_observation_not_effective_date",
        "effective_date_asserted": False,
        "official_only": True,
    }
    state = next(row for row in state_rows
                 if row["state_sha256"] == transition["state_sha256"])
    assert state["norms"][0]["text"] == "neue Fassung"
    assert state["provenance"]["verification"] == "exact_complete_state"


def test_hf_history_rejects_review_without_captured_state_pair(
        tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    manifest = archive_gii_states([
        _snapshot(snapshots, "2026-07-14", "alt"),
        _snapshot(snapshots, "2026-07-15", "neu"),
    ], store)
    transition = transitions(manifest, store)[0]
    transition["state_sha256"] = "f" * 64
    reviews = tmp_path / "reviews.json"
    _review_file(reviews, transition)

    with pytest.raises(RuntimeError, match="has no state transition"):
        _official_history_rows(store, reviews)
