from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.procedure_search import search_procedures


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
    }]}}}


def test_searches_official_fields_and_watch_aliases() -> None:
    for query in ("Ukraine", "Rechtskreiswechsel", "Fiktion", "G013"):
        hits = search_procedures(_hierarchy(), query)
        assert [hit["id"] for hit in hits] == ["329468"]
        assert hits[0]["status"] == "Überwiesen"
        assert hits[0]["source"] == "DIP"


def test_does_not_invent_unrelated_matches() -> None:
    assert search_procedures(_hierarchy(), "Mietrecht") == []
