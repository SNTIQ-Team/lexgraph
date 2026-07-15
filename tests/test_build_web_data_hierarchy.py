from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402


def test_public_build_does_not_read_quarantined_snapshots(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEXGRAPH_INCLUDE_QUARANTINED", raising=False)

    def forbidden(_source: str) -> Path:
        raise AssertionError("quarantined snapshot lookup must not happen")

    monkeypatch.setattr(web_data, "latest_snapshot", forbidden)
    for source in web_data.QUARANTINED_SOURCES:
        assert web_data.load(source, "rows.jsonl") == []


def test_quarantined_snapshot_requires_explicit_private_opt_in(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "rows.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    monkeypatch.setenv("LEXGRAPH_INCLUDE_QUARANTINED", "1")
    monkeypatch.setattr(web_data, "latest_snapshot", lambda _source: snapshot)

    assert web_data.load("buzer", "rows.jsonl") == [{"id": 1}]


def test_legal_layers_separate_constitutions_statutes_and_ordinances() -> None:
    acts = [
        {"id": "fed_gg", "jurabk": "GG", "title": "Grundgesetz für die Bundesrepublik Deutschland"},
        {"id": "fed_aufenthg", "jurabk": "AufenthG", "title": "Gesetz über den Aufenthalt"},
        {"id": "fed_aufenthv", "jurabk": "AufenthV", "title": "Aufenthaltsverordnung"},
        {"id": "by_bayverf", "jurabk": "BayVerf", "title": "Verfassung des Freistaates Bayern"},
        {"id": "by_lstvg", "jurabk": "LStVG", "title": "Gesetz über das Verordnungsrecht"},
        {"id": "by_dvasyl", "jurabk": "DVAsyl", "title": "Verordnung zur Durchführung des Asylgesetzes"},
    ]

    layers = web_data._legal_layers(acts)

    assert [act["id"] for act in layers["constitution"]] == [
        "fed_gg", "by_bayverf"]
    assert [act["id"] for act in layers["statutes"]] == [
        "fed_aufenthg", "by_lstvg"]
    assert [act["id"] for act in layers["ordinances"]] == [
        "fed_aufenthv", "by_dvasyl"]
    assert sum(map(len, layers.values())) == len(acts)


def test_hierarchy_v2_keeps_flat_lists_and_adds_legal_layers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    instruments = [
        {"celex": "32024L0001", "kind": "directive", "title": "RL",
         "in_force": True, "in_geas_core": False},
        {"celex": "32024R0002", "kind": "regulation", "title": "VO",
         "in_force": True, "in_geas_core": True},
    ]

    def fake_load(source: str, name: str) -> list[dict]:
        if (source, name) == ("eu_layer", "instruments.jsonl"):
            return instruments
        if (source, name) == ("eu_layer", "transpositions.jsonl"):
            return [{"directive_celex": "32024L0001"}]
        return []

    monkeypatch.setattr(web_data, "load", fake_load)
    wiki = [
        {"id": "fed_gg", "jurabk": "GG", "juris": "DE",
         "title": "Grundgesetz für die Bundesrepublik Deutschland"},
        {"id": "fed_intv", "jurabk": "IntV", "juris": "DE",
         "title": "Verordnung über Integrationskurse"},
        {"id": "by_bayverf", "jurabk": "BayVerf", "juris": "DE-BY",
         "title": "Verfassung des Freistaates Bayern"},
    ]

    hierarchy = web_data.build_hierarchy(wiki)

    assert hierarchy["meta"] == {
        "schema_version": 2,
        "model": "competence-aware",
        "not_a_total_order": True,
        "coverage": {
            "laender_monitor": "origin_verification_required",
            "laender_keys": 16,
        },
    }
    assert hierarchy["eu"]["instruments"] == (
        hierarchy["eu"]["secondary"]["directives"]
        + hierarchy["eu"]["secondary"]["regulations"])
    assert hierarchy["eu"]["primary"]["indexed"] is False
    assert len(hierarchy["eu"]["primary"]["references"]) == 3
    assert all(reference["in_corpus"] is False
               for reference in hierarchy["eu"]["primary"]["references"])
    assert hierarchy["eu"]["secondary"]["directives"][0]["deu_mnes"] == 1
    assert hierarchy["bund"]["acts"] == wiki[:2]
    assert hierarchy["bund"]["layers"]["constitution"] == [wiki[0]]
    assert hierarchy["bund"]["layers"]["ordinances"] == [wiki[1]]
    assert hierarchy["bayern"]["layers"]["constitution"] == [wiki[2]]
    assert set(hierarchy["laender"]) == set(
        web_data.ALL_LAENDER_JURISDICTIONS)
    assert hierarchy["laender"]["DE-HB"] == []


def test_hierarchy_keeps_full_searchable_dip_procedure_and_watch_metadata(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    long_title = ("Gesetz zur Änderung der Gewährung von Leistungen für "
                  "Personen, die vorübergehenden Schutz beantragt haben "
                  "(Leistungsrechtsanpassungsgesetz)")
    procedure = {
        "id": "329468", "titel": long_title, "datum": "2026-02-11",
        "aktualisiert": "2026-02-12T10:57:00+01:00",
        "beratungsstand": "Überwiesen", "gesta": "G013",
        "sachgebiet": ["Soziale Sicherung"],
        "initiative": ["Bundesregierung"],
        "deskriptor": [{"name": "Ukraine"}],
        "abstract": "Rechtskreiswechsel<br />SGB II &amp; AsylbLG",
    }

    def fake_load(source: str, name: str) -> list[dict]:
        return [procedure] if (source, name) == ("dip", "vorgaenge.jsonl") else []

    watch = tmp_path / "watch.json"
    watch.write_text('{"procedures":{"329468":{"queries":["Fiktion"]}}}',
                     encoding="utf-8")
    monkeypatch.setattr(web_data, "load", fake_load)
    monkeypatch.setattr(web_data, "PROCEDURE_WATCHLIST", watch)

    hierarchy = web_data.build_hierarchy([])
    row = hierarchy["bund"]["pipeline"]["Überwiesen"][0]
    assert row["title"] == long_title
    assert row["summary"] == "Rechtskreiswechsel SGB II & AsylbLG"
    assert row["descriptors"] == ["Ukraine"]
    assert row["watched"] is True
    assert row["watch"]["queries"] == ["Fiktion"]
    assert row["url"].endswith("/329468")


def test_hierarchy_keeps_watched_eu_procedure_separate_from_law_in_force(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    eu_watch = {
        "id": "eu-2026-0186-nle", "procedure": "2026/0186/NLE",
        "proposal_celex": "52026PC0345", "title": "Proposal extending temporary protection",
        "date": "2026-06-26", "fetched_at": "2026-07-15T02:00:00Z",
        "status": "Ongoing", "stage": "Discussions within the Council",
        "terminal": False, "events": [],
        "url": "https://eur-lex.europa.eu/procedure/EN/2026_186",
    }

    def fake_load(source: str, name: str) -> list[dict]:
        return [eu_watch] if (source, name) == (
            "eu_watch", "procedures.jsonl") else []

    watch = tmp_path / "watch.json"
    watch.write_text(json.dumps({"procedures": {"eu-2026-0186-nle": {
        "source": "EUR-Lex", "queries": ["military obligations"],
        "scope": "Draft scope",
    }}}), encoding="utf-8")
    monkeypatch.setattr(web_data, "load", fake_load)
    monkeypatch.setattr(web_data, "PROCEDURE_WATCHLIST", watch)

    hierarchy = web_data.build_hierarchy([])
    row = hierarchy["eu"]["pipeline"]["Ongoing"][0]
    assert row["source"] == "EUR-Lex"
    assert row["procedure"] == "2026/0186/NLE"
    assert row["stage"] == "Discussions within the Council"
    assert row["watched"] is True
    assert row["terminal"] is False
    assert hierarchy["eu"]["instruments"] == []
