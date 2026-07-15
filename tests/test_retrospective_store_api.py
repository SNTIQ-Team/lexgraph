from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from api.retrospective_store import (  # noqa: E402
    RetrospectiveAmbiguity,
    RetrospectiveNotFound,
    diff_intervals,
    resolve_interval,
    validate_manifest,
)
from official_states import archive_gii_states  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _state(snapshots: Path, day: str, text: str,
           extra: list[dict] | None = None) -> Path:
    path = snapshots / day
    norms = [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 1",
        "titel": "Zweck", "text": text, "doknr": "N1",
        "gliederung": "Teil 1",
    }, *(extra or [])]
    _write_jsonl(path / "acts.jsonl", [{
        "slug": "demog", "jurabk": "DemoG",
        "long_title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "builddate": f"{day.replace('-', '')}010101",
        "doknr": "A1", "norm_count": len(norms),
    }])
    _write_jsonl(path / "norms.jsonl", norms)
    return path


def _fixture(tmp_path: Path) -> tuple[dict, Path, str, str]:
    snapshots, store = tmp_path / "snapshots", tmp_path / "states"
    added = [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 2",
        "titel": "Neu", "text": "zweiter Text", "doknr": "N2",
        "gliederung": "Teil 1",
    }]
    manifest = archive_gii_states([
        _state(snapshots, "2026-01-02", "neu", added),
        _state(snapshots, "2025-01-02", "alt"),
    ], store)
    observations = sorted(manifest["observations"],
                          key=lambda row: row["observed_at"])
    old, new = (row["state_sha256"] for row in observations)
    retro = {
        "schema_version": 1,
        "kind": "lexgraph-retrospective-history",
        "built_at": "2026-07-16T12:00:00Z",
        "state_identity": "sha256-canonical-uncompressed-json",
        "objects": manifest["objects"],
        "date_semantics": {
            "effective": "[effective_from,effective_to)",
            "knowledge": "[knowledge_from,knowledge_to)",
        },
        "acts": {"fed_demog": {
            "act_id": "fed_demog", "jurabk": "DemoG",
            "title": "Demonstrationsgesetz", "events": [],
            "observations": observations, "gaps": [],
            "intervals": [{
                "id": "old", "effective_from": "2024-01-01",
                "effective_to": "2025-01-01",
                "knowledge_from": "2026-07-16T10:00:00Z",
                "knowledge_to": None, "published_at": "2023-12-20",
                "observed_at": "2025-01-02", "state_sha256": old,
                "verified_through_observed_at": "2025-01-02",
                "previous_state_sha256": None,
                "text_status": "official_exact",
                "date_status": "official_verified",
                "date_basis": "official_bgbl_commencement_clause",
                "verification": "test", "gaps": [], "evidence": [],
            }, {
                "id": "new", "effective_from": "2025-01-01",
                "effective_to": None,
                "knowledge_from": "2026-07-16T10:00:00Z",
                "knowledge_to": None, "published_at": "2024-12-20",
                "observed_at": "2026-01-02", "state_sha256": new,
                "verified_through_observed_at": "2026-01-02",
                "previous_state_sha256": old,
                "text_status": "official_exact",
                "date_status": "official_verified",
                "date_basis": "official_bgbl_commencement_clause",
                "verification": "test", "gaps": [], "evidence": [],
            }],
        }},
    }
    return retro, store, old, new


def test_half_open_effective_boundaries_and_default_as_of(tmp_path: Path):
    raw, _store, old, new = _fixture(tmp_path)
    manifest = validate_manifest(raw)

    assert resolve_interval(
        manifest, "fed_demog", "2024-12-31")["state_sha256"] == old
    assert resolve_interval(
        manifest, "fed_demog", "2025-01-01")["state_sha256"] == new
    with pytest.raises(RetrospectiveNotFound):
        resolve_interval(manifest, "fed_demog", "2023-12-31")


def test_later_correction_is_hidden_from_earlier_knowledge(tmp_path: Path):
    raw, _store, old, new = _fixture(tmp_path)
    original = raw["acts"]["fed_demog"]["intervals"][1]
    original["knowledge_to"] = "2026-07-16T11:00:00Z"
    corrected = {
        **original,
        "id": "new-corrected",
        "effective_from": "2025-02-01",
        "knowledge_from": "2026-07-16T11:00:00Z",
        "knowledge_to": None,
    }
    raw["acts"]["fed_demog"]["intervals"].append(corrected)
    manifest = validate_manifest(raw)

    assert resolve_interval(
        manifest, "fed_demog", "2025-01-15",
        as_of="2026-07-16T10:30:00Z")["state_sha256"] == new
    with pytest.raises(RetrospectiveNotFound):
        resolve_interval(manifest, "fed_demog", "2025-01-15",
                         as_of="2026-07-16T11:30:00Z")


def test_ambiguous_overlap_at_one_knowledge_slice_fails(tmp_path: Path):
    raw, _store, _old, _new = _fixture(tmp_path)
    overlap = {
        **raw["acts"]["fed_demog"]["intervals"][0],
        "id": "overlap", "effective_from": "2024-06-01",
        "effective_to": "2025-03-01",
    }
    raw["acts"]["fed_demog"]["intervals"].append(overlap)
    with pytest.raises(RetrospectiveAmbiguity, match="overlapping"):
        validate_manifest(raw)


def test_diff_add_replace_and_norm_filter(tmp_path: Path):
    raw, store, _old, _new = _fixture(tmp_path)
    manifest = validate_manifest(raw)
    result = diff_intervals(
        manifest, "fed_demog", "2024-06-01", "2025-06-01",
        store=store)

    assert result["exact"] is True
    assert [(row["operation"], row["enbez"])
            for row in result["changes"]] == [
                ("replace", "§ 1"), ("add", "§ 2")]
    section = diff_intervals(
        manifest, "fed_demog", "2024-06-01", "2025-06-01",
        norm="§ 1", store=store)
    assert len(section["changes"]) == 1
    assert section["changes"][0]["old"] == "alt"
    assert section["changes"][0]["new"] == "neu"


def test_official_retroactive_date_is_preserved_not_rejected(tmp_path: Path):
    raw, _store, _old, _new = _fixture(tmp_path)
    raw["acts"]["fed_demog"]["intervals"][0].update({
        "effective_from": "2023-12-01",
        "published_at": "2023-12-20",
    })
    manifest = validate_manifest(raw)
    assert manifest["acts"]["fed_demog"]["intervals"][0]["retroactive"] is True


def test_open_interval_stops_at_last_verified_observation(tmp_path: Path):
    raw, _store, _old, new = _fixture(tmp_path)
    manifest = validate_manifest(raw)
    assert resolve_interval(
        manifest, "fed_demog", "2026-01-02")["state_sha256"] == new
    with pytest.raises(RetrospectiveNotFound):
        resolve_interval(manifest, "fed_demog", "2026-01-03")


def test_future_knowledge_time_is_not_claimed(tmp_path: Path):
    raw, _store, _old, _new = _fixture(tmp_path)
    manifest = validate_manifest(raw)
    with pytest.raises(RetrospectiveNotFound, match="knowledge horizon"):
        resolve_interval(
            manifest, "fed_demog", "2025-01-01",
            as_of="2026-07-16T12:00:01Z")
