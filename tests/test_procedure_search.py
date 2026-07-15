from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.procedure_search import ProcedureSearchIndex, search_procedures


def _hierarchy() -> dict:
    return {"bund": {"pipeline": {"Überwiesen": [{
        "id": "329468",
        "title": "Gesetz zur Änderung der Gewährung von Leistungen (Leistungsrechtsanpassungsgesetz)",
        "date": "2026-02-11",
        "updated": "2026-02-12T10:57:00+01:00",
        "status": "Überwiesen",
        "gesta": "G013",
        "topics": ["Migration und Aufenthaltsrecht", "Soziale Sicherung"],
        "initiators": ["Bundesregierung"],
        "descriptors": ["Ukraine", "Bürgergeld"],
        "summary": "Regelungen zum Rechtskreiswechsel ukrainischer Geflüchteter",
        "watched": True,
        "watch": {"queries": ["Fiktionsbescheinigung", "§ 24 AufenthG"]},
        "url": "https://dip.bundestag.de/vorgang/_/329468",
    }]}}, "eu": {"pipeline": {"Ongoing": [{
        "id": "eu-2026-0186-nle",
        "procedure": "2026/0186/NLE",
        "proposal_celex": "52026PC0345",
        "title": "Proposal extending temporary protection",
        "date": "2026-06-26",
        "status": "Ongoing",
        "stage": "Discussions within the Council",
        "watched": True,
        "watch": {
            "queries": ["military obligations", "Ausreisegenehmigung"],
            "scope": "Collective temporary protection for Ukraine",
        },
        "url": "https://eur-lex.europa.eu/procedure/EN/2026_186",
    }]}}}


def test_searches_official_fields_and_watch_aliases() -> None:
    for query in ("Rechtskreiswechsel", "Fiktion", "G013"):
        hits = search_procedures(_hierarchy(), query)
        assert [hit["id"] for hit in hits] == ["329468"]
        assert hits[0]["status"] == "Überwiesen"
        assert hits[0]["source"] == "DIP"
    assert {hit["id"] for hit in search_procedures(
        _hierarchy(), "Ukraine")} == {"329468", "eu-2026-0186-nle"}


def test_does_not_invent_unrelated_matches() -> None:
    assert search_procedures(_hierarchy(), "Mietrecht") == []


def test_searches_eurlex_identifiers_and_watch_aliases() -> None:
    for query in ("2026/0186/NLE", "52026PC0345", "Ausreisegenehmigung"):
        hits = search_procedures(_hierarchy(), query)
        assert [hit["id"] for hit in hits] == ["eu-2026-0186-nle"]
        assert hits[0]["source"] == "EUR-Lex"
        assert hits[0]["status"] == "Ongoing"


def test_prepared_index_reuses_normalized_procedure_fields() -> None:
    hierarchy = _hierarchy()
    index = ProcedureSearchIndex(hierarchy)

    assert [row["id"] for row in index.search("Fiktionsbescheinigung")] == [
        "329468"]
    assert index.source_hierarchy is hierarchy
