from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402


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
