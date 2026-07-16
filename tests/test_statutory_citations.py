from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.statutory_citations import (
    build_citation_index,
    citation_manifest,
    write_citation_database,
)


def _acts() -> list[dict]:
    return [{
        "id": "fed_alpha", "jurabk": "AlphaG", "juris": "DE",
        "title": "Alpha-Gesetz",
        "norms": [{
            "enbez": "§ 1", "text": (
                "Nach § 2 gilt die Regel; § 2 gilt nochmals. "
                "Die §§ 3 bis 4 bleiben unberührt. "
                "Die § 5 und § 6 BetaG sind anzuwenden. "
                "Außerdem gelten § 999 BetaG und § 1 UnknownG. "
                "Art. 1 ist hier kein Paragraph des AlphaG."
            ),
        }, {
            "enbez": "§ 2", "text": "Nach § 1 dieses Gesetzes gilt dies.",
        }, {
            "enbez": "§ 3", "text": "",
        }, {
            "enbez": "§ 4", "text": "",
        }],
    }, {
        "id": "fed_beta", "jurabk": "BetaG", "juris": "DE",
        "title": "Beta-Gesetz",
        "norms": [
            {"enbez": "§ 5", "text": ""},
            {"enbez": "§ 6", "text": ""},
        ],
    }, {
        "id": "fed_gg", "jurabk": "GG", "juris": "DE",
        "title": "Grundgesetz für die Bundesrepublik Deutschland",
        "norms": [{"enbez": "Art 1", "text": ""}],
    }]


def _rows() -> tuple[dict, list[dict]]:
    index = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00",
        source_snapshots={"DE": "2026-07-15"},
    )
    return index, index["citations"]


def _only_alpha_text(acts: list[dict], text: str) -> None:
    for norm in acts[0]["norms"]:
        norm["text"] = ""
    acts[0]["norms"][0]["text"] = text


def test_current_citation_index_separates_self_cross_and_unresolved():
    index, rows = _rows()

    self_targets = {(row["target_norm"], row["status"])
                    for row in rows
                    if row["source_act"] == "fed_alpha"
                    and row["source_norm"] == "§ 1"
                    and row["kind"] == "self"}
    assert {("§ 2", "resolved"), ("§ 3", "resolved"),
            ("§ 4", "resolved")} <= self_targets

    beta = [row for row in rows if row["target_act"] == "fed_beta"]
    assert {(row["target_norm"], row["kind"], row["status"])
            for row in beta} == {
                ("§ 5", "cross_act", "resolved"),
                ("§ 6", "cross_act", "resolved"),
                ("§ 999", "cross_act", "unresolved"),
            }
    missing_norm = next(row for row in beta if row["target_norm"] == "§ 999")
    assert missing_norm["unresolved_reason"] == \
        "target_norm_not_in_current_corpus"

    unknown = next(row for row in rows
                   if row["target_jurabk"] == "UnknownG")
    assert unknown["target_act"] is None
    assert unknown["kind"] == "cross_act"
    assert unknown["status"] == "unresolved"
    assert unknown["unresolved_reason"] == \
        "target_act_not_in_current_corpus"

    foreign_marker = next(row for row in rows
                          if row["target_norm"] == "Art. 1")
    assert foreign_marker["status"] == "unresolved"
    assert foreign_marker["unresolved_reason"] == \
        "unqualified_foreign_marker"

    repeated = next(row for row in rows
                    if row["source_act"] == "fed_alpha"
                    and row["source_norm"] == "§ 1"
                    and row["target_act"] == "fed_alpha"
                    and row["target_norm"] == "§ 2")
    assert repeated["occurrence_count"] == 2
    assert repeated["source_excerpt"]
    assert repeated["machine_extracted"] is True
    assert repeated["current_state_only"] is True
    assert repeated["legal_interpretation"] == "not_asserted"
    assert repeated["source_snapshot"] == "2026-07-15"
    assert repeated["date_basis"] == \
        "current_consolidated_snapshot_observation_not_legal_effect"

    assert index["machine_extracted"] is True
    assert index["current_state_only"] is True
    assert index["source_policy"]["fuzzy_matching"] is False
    assert index["counts"]["total"] == len(rows)
    assert index["counts"]["unresolved"] >= 3


def test_shared_trailing_alias_applies_to_every_explicit_head():
    acts = _acts()
    _only_alpha_text(acts, "Nach § 5 und § 6 BetaG gilt dies.")
    rows = build_citation_index(
        acts, built_at="2026-07-16T12:00:00+00:00")['citations']

    assert {(row["target_act"], row["target_norm"], row["kind"])
            for row in rows} == {
                ("fed_beta", "§ 5", "cross_act"),
                ("fed_beta", "§ 6", "cross_act"),
            }


def test_article_dot_normalization_and_exact_alias_resolution():
    acts = _acts()
    _only_alpha_text(acts, "Die Garantie aus Art. 1 GG gilt.")
    rows = build_citation_index(
        acts, built_at="2026-07-16T12:00:00+00:00")['citations']

    assert len(rows) == 1
    assert rows[0]["target_act"] == "fed_gg"
    assert rows[0]["target_norm"] == "Art. 1"
    assert rows[0]["status"] == "resolved"


def test_ambiguous_exact_alias_is_flagged_instead_of_guessed():
    acts = _acts() + [{
        "id": "by_beta", "jurabk": "BetaG", "juris": "DE-BY",
        "title": "Anderes Beta-Gesetz",
        "norms": [{"enbez": "Art. 5", "text": ""}],
    }]
    _only_alpha_text(acts, "Siehe § 5 BetaG.")
    rows = build_citation_index(
        acts, built_at="2026-07-16T12:00:00+00:00")['citations']

    assert len(rows) == 1
    assert rows[0]["target_act"] is None
    assert rows[0]["status"] == "unresolved"
    assert rows[0]["unresolved_reason"] == "ambiguous_target_act"


def test_ids_and_output_order_are_deterministic():
    first = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00")
    second = build_citation_index(
        list(reversed(_acts())), built_at="2026-07-16T12:00:00+00:00")

    assert first["citations"] == second["citations"]


def test_sqlite_is_deterministic_indexed_and_manifest_has_no_rows(
        tmp_path: Path):
    index = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00",
        source_snapshots={"DE": "2026-07-15"})
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    write_citation_database(first, index)
    write_citation_database(second, index)

    assert first.read_bytes() == second.read_bytes()
    manifest = citation_manifest(index)
    assert "citations" not in manifest
    assert manifest["storage"] == {
        "format": "sqlite3", "file": "citations.sqlite",
        "table": "citation", "rows": index["counts"]["total"],
        "ordering": "ordinal", "read_only": True,
    }
    assert len(json.dumps(manifest)) < 5000

    with sqlite3.connect(first) as database:
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert database.execute(
            "SELECT COUNT(*) FROM citation").fetchone()[0] == \
            index["counts"]["total"]
        indexes = {row[1] for row in database.execute(
            "PRAGMA index_list('citation')")}
        assert {"citation_source_act", "citation_source_jurabk",
                "citation_target_act", "citation_target_jurabk",
                "citation_target_pinpoint"} <= indexes
        plan = " ".join(str(part) for row in database.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM citation "
            "WHERE target_act_key=? AND target_norm_key=? ORDER BY ordinal",
            ("fed_beta", "§ 5")) for part in row)
        assert "citation_target_act" in plan


def test_citations_api_exact_direction_filters_and_stable_pagination(
        tmp_path: Path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from api import main as api_main
    from fastapi.testclient import TestClient

    data = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00",
        source_snapshots={"DE": "2026-07-15"})
    manifest = citation_manifest(data)
    write_citation_database(tmp_path / "citations.sqlite", data)
    monkeypatch.setattr(api_main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api_main, "_load", lambda name: manifest
                        if name == "citations" else None)

    inbound = api_main.citations(
        act="BetaG", norm="§ 5", direction="in", kind="cross_act",
        limit=100, offset=0)
    assert inbound["direction"] == "in"
    assert inbound["matched"] == 1
    assert inbound["citations"][0]["target_act"] == "fed_beta"
    assert inbound["citations"][0]["target_norm"] == "§ 5"

    outbound = api_main.citations(
        act="fed_alpha", norm=None, direction="out", kind=None,
        limit=1000, offset=0)
    first_page = api_main.citations(
        act="fed_alpha", norm=None, direction="out", kind=None,
        limit=2, offset=0)
    second_page = api_main.citations(
        act="fed_alpha", norm=None, direction="out", kind=None,
        limit=2, offset=2)
    assert first_page["citations"] + second_page["citations"] == \
        outbound["citations"][:4]
    assert {row["id"] for row in first_page["citations"]}.isdisjoint(
        {row["id"] for row in second_page["citations"]})

    exact = api_main.citations(
        act="fed_alpha", norm="§ 2", direction="out", kind="self",
        limit=100, offset=0)
    assert exact["matched"] == 1
    assert exact["citations"][0]["source_norm"] == "§ 2"

    client = TestClient(api_main.app)
    response = client.get("/citations", params={
        "act": "BetaG", "norm": "§ 5", "direction": "in",
        "kind": "cross_act", "limit": 1, "offset": 0,
    })
    assert response.status_code == 200
    assert response.json()["citations"][0]["target_act"] == "fed_beta"
    assert client.get("/citations?direction=sideways").status_code == 422
