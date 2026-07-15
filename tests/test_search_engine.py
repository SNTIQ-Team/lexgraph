from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.search_engine import (  # noqa: E402
    SearchEngine,
    build_search_database,
    normalize_search_text,
)


SYNONYMS = ROOT / "data" / "search_synonyms.json"


@pytest.fixture()
def engine(tmp_path: Path) -> SearchEngine:
    details = {
        "fed_ukraineaufenth_v": {
            "id": "fed_ukraineaufenth_v",
            "jurabk": "UkraineAufenthÜV",
            "juris": "DE",
            "title": "Verordnung zur vorübergehenden Befreiung vom "
                     "Erfordernis eines Aufenthaltstitels von anlässlich "
                     "des Krieges in der Ukraine eingereisten Personen",
            "norms": [
                {"enbez": "§ 1", "titel": "Gegenstand",
                 "text": "Diese Verordnung regelt die Einreise aus der Ukraine."},
                {"enbez": "§ 2", "titel": "Befreiung",
                 "text": "Vorübergehende Befreiung vom Aufenthaltstitel."},
            ],
        },
        "fed_ukraineaufenthfgv": {
            "id": "fed_ukraineaufenthfgv",
            "jurabk": "UkraineAufenthFGV",
            "juris": "DE",
            "title": "Verordnung zur Fortgeltung der Aufenthaltserlaubnisse "
                     "für vorübergehend Schutzberechtigte aus der Ukraine",
            "norms": [
                {"enbez": "§ 1", "titel": "Gegenstand",
                 "text": "Fortgeltung wegen des Krieges in der Ukraine."},
                {"enbez": "§ 2", "titel": "Fortgeltung",
                 "text": "Die Aufenthaltserlaubnis gilt fort."},
            ],
        },
        "fed_aufenthg_2004": {
            "id": "fed_aufenthg_2004",
            "jurabk": "AufenthG 2004",
            "juris": "DE",
            "title": "Gesetz über den Aufenthalt, die Erwerbstätigkeit und "
                     "die Integration von Ausländern im Bundesgebiet",
            "norms": [{
                "enbez": "§ 24",
                "titel": "Aufenthaltsgewährung zum vorübergehenden Schutz",
                "text": "Einem Ausländer kann zum vorübergehenden Schutz "
                        "eine Aufenthaltserlaubnis erteilt werden."
            }, {
                "enbez": "§ 249a", "titel": "Andere Aufenthaltsregelung",
                "text": "Diese Vorschrift ist für die Suche nicht einschlägig."
            }, {
                "enbez": "§ 81", "titel": "Beantragung des Aufenthaltstitels",
                "text": "Eine Fiktionsbescheinigung ist auszustellen."
            }],
        },
        "fed_sgb_2": {
            "id": "fed_sgb_2", "jurabk": "SGB 2", "juris": "DE",
            "title": "Sozialgesetzbuch Zweites Buch",
            "norms": [{"enbez": "§ 74", "titel": "Ansprüche mit einer "
                       "Fiktionsbescheinigung", "text": "Leistungen bei "
                       "beantragter Aufenthaltserlaubnis nach § 24."}],
        },
        "fed_sgb_3": {
            "id": "fed_sgb_3", "jurabk": "SGB 3", "juris": "DE",
            "title": "Sozialgesetzbuch Drittes Buch (III)",
            "norms": [
                {"enbez": "§ 74", "titel": "Assistierte Ausbildung",
                 "text": "Ausbildungsbegleitende Unterstützung."},
                {"enbez": "§ 99", "titel": "Nur ein Texttreffer",
                 "text": "Ukraine steht hier ohne fachlichen Bezug."},
            ],
        },
        "fed_sgb_12": {
            "id": "fed_sgb_12", "jurabk": "SGB 12", "juris": "DE",
            "title": "Sozialgesetzbuch Zwölftes Buch",
            "norms": [{"enbez": "§ 146", "titel": "Sozialhilfe mit "
                       "Aufenthaltstitel nach § 24", "text": "Sozialhilfe."}],
        },
        "fed_sgb_5": {
            "id": "fed_sgb_5", "jurabk": "SGB 5", "juris": "DE",
            "title": "Sozialgesetzbuch Fünftes Buch",
            "norms": [{"enbez": "§ 417", "titel": "Versicherung mit "
                       "Aufenthaltserlaubnis nach § 24", "text": "Beitritt."}],
        },
        "fed_sgb_9_2018": {
            "id": "fed_sgb_9_2018", "jurabk": "SGB 9 2018",
            "juris": "DE", "title": "Sozialgesetzbuch Neuntes Buch",
            "norms": [{"enbez": "§ 150a", "titel": "Übergangsregelung",
                       "text": "Aufenthaltstitel nach § 24."}],
        },
        "fed_asylblg": {
            "id": "fed_asylblg", "jurabk": "AsylbLG", "juris": "DE",
            "title": "Asylbewerberleistungsgesetz",
            "norms": [
                {"enbez": "§ 1", "titel": "Leistungsberechtigte",
                 "text": "Schutzgesuch und Fiktionsbescheinigung."},
                {"enbez": "§ 6", "titel": "Sonstige Leistungen",
                 "text": "Besondere Bedürfnisse bei § 24."},
                {"enbez": "§ 18", "titel": "Übergangsregelung",
                 "text": "Aufenthaltserlaubnis nach § 24."},
            ],
        },
        "by_bayverf": {
            "id": "by_bayverf", "jurabk": "BayVerf", "juris": "DE-BY",
            "title": "Verfassung des Freistaates Bayern",
            "norms": [{"enbez": "Art. 1", "titel": "Freistaat Bayern",
                       "text": "Bayern ist ein Freistaat."}],
        },
    }
    wiki = [{key: act[key] for key in ("id", "jurabk", "juris", "title")}
            for act in details.values()]
    path = tmp_path / "search.sqlite"
    counts = build_search_database(details, path, SYNONYMS)
    assert counts == {"acts": 10, "norms": 17}
    search = SearchEngine(path, wiki)
    yield search
    search.close()


def test_unicode_normalization_preserves_scripts_and_folds_german() -> None:
    assert normalize_search_text("  ÜBER Straße — Україна / Россия ") == \
        "uber strasse україна россия"


def test_build_rejects_stale_curated_targets(tmp_path: Path) -> None:
    output = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match=r"fed_ukraineaufenth_v"):
        build_search_database({}, output, SYNONYMS)
    assert not output.exists()


@pytest.mark.parametrize(
    "query", ["Ukraine", "Украина", "Україна", "ukrainisch",
              "temporary protection"])
def test_multilingual_ukraine_aliases_find_acts_and_relevant_norm(
        engine: SearchEngine, query: str) -> None:
    result = engine.search(query, act_limit=10, norm_limit=10)

    assert {row["jurabk"] for row in result["act_matches"]} == {
        "UkraineAufenthÜV", "UkraineAufenthFGV"}
    assert any(row["jurabk"] == "AufenthG 2004"
               and row["enbez"] == "§ 24"
               for row in result["norm_matches"])
    assert (result["norm_matches"][0]["jurabk"],
            result["norm_matches"][0]["enbez"]) == \
        ("AufenthG 2004", "§ 24")
    assert result["result_total"] == \
        result["act_total"] + result["norm_total"]
    assert all("<" not in row["snippet"] for row in result["norm_matches"])


def test_ukraine_concept_priority_precedes_title_and_body_matches(
        engine: SearchEngine) -> None:
    result = engine.search("Ukraine", act_limit=10, norm_limit=50)
    rows = result["norm_matches"]
    positions = {(row["jurabk"], row["enbez"]): position
                 for position, row in enumerate(rows)}
    transition_norms = {
        ("SGB 2", "§ 74"),
        ("SGB 12", "§ 146"),
        ("SGB 9 2018", "§ 150a"),
        ("SGB 5", "§ 417"),
        ("AsylbLG", "§ 18"),
        ("AufenthG 2004", "§ 81"),
    }
    ukraine_regulations = {
        ("UkraineAufenthÜV", "§ 1"),
        ("UkraineAufenthÜV", "§ 2"),
        ("UkraineAufenthFGV", "§ 1"),
        ("UkraineAufenthFGV", "§ 2"),
    }

    assert positions[("AufenthG 2004", "§ 24")] == 0
    assert transition_norms <= positions.keys()
    assert ukraine_regulations <= positions.keys()
    # Curated priority 3 transition norms cannot be displaced by the many
    # literal Ukraine fields on priority 2 regulation norms.
    assert max(positions[key] for key in transition_norms) < \
        min(positions[key] for key in ukraine_regulations)
    first_plain_text = next(
        position for position, row in enumerate(rows)
        if "concept" not in row["matched_fields"])
    assert max(positions[key] for key in transition_norms |
               ukraine_regulations) < first_plain_text


@pytest.mark.parametrize("query", [
    "Fiktionsbescheinigung",
    "Fiktionswirkung",
    "Aufenthaltsfiktion",
])
def test_fiktionsbescheinigung_concept_prioritizes_transition_norms(
        engine: SearchEngine, query: str) -> None:
    result = engine.search(query, act_limit=10, norm_limit=50)
    rows = result["norm_matches"]
    required = {
        ("AufenthG 2004", "§ 81"),
        ("SGB 2", "§ 74"),
        ("SGB 12", "§ 146"),
        ("SGB 9 2018", "§ 150a"),
        ("SGB 5", "§ 417"),
        ("AsylbLG", "§ 18"),
    }
    positions = {(row["jurabk"], row["enbez"]): position
                 for position, row in enumerate(rows)}

    assert required <= positions.keys()
    assert positions[("AufenthG 2004", "§ 81")] == 0
    assert all("concept" in rows[positions[key]]["matched_fields"]
               for key in required)
    # AsylbLG § 1 contains the word in its body but is not one of the
    # curated transitional targets for this concept.
    if ("AsylbLG", "§ 1") in positions:
        assert max(positions[key] for key in required) < \
            positions[("AsylbLG", "§ 1")]


@pytest.mark.parametrize("query", [
    "Rechtskreiswechsel",
    "Rechtskreiswechsel Ukraine",
    "Leistungssystemwechsel Ukraine",
    "Wechsel vom SGB II zum AsylbLG",
    "Leistungsrechtsanpassungsgesetz",
])
def test_rechtskreiswechsel_ranks_social_transition_before_residence_norms(
        engine: SearchEngine, query: str) -> None:
    result = engine.search(query, act_limit=10, norm_limit=50)
    rows = result["norm_matches"]
    positions = {(row["jurabk"], row["enbez"]): position
                 for position, row in enumerate(rows)}
    social = {
        ("SGB 2", "§ 74"),
        ("SGB 12", "§ 146"),
        ("SGB 9 2018", "§ 150a"),
        ("SGB 5", "§ 417"),
        ("AsylbLG", "§ 18"),
    }
    residence = {
        ("AufenthG 2004", "§ 24"),
        ("AufenthG 2004", "§ 81"),
    }

    assert social | residence <= positions.keys()
    assert {key for key, position in positions.items() if position < 2} == {
        ("SGB 2", "§ 74"), ("SGB 12", "§ 146")}
    assert max(positions[key] for key in social) < \
        min(positions[key] for key in residence)
    assert all("concept" in row["matched_fields"] for row in rows)


@pytest.mark.parametrize(
    "query", ["BayVerf", "Bayerische Verfassung", "Bavarian constitution",
              "Конституция Баварии", "Конституція Баварії"])
def test_multilingual_bavarian_constitution_aliases_find_act(
        engine: SearchEngine, query: str) -> None:
    result = engine.search(query, act_limit=10, norm_limit=10)

    assert result["act_matches"][0]["id"] == "by_bayverf"


def test_norm_query_ranks_exact_section_first(engine: SearchEngine) -> None:
    result = engine.search("§ 24 Aufenthalt", act_limit=10, norm_limit=10)

    assert result["norm_matches"][0]["jurabk"] == "AufenthG 2004"
    assert result["norm_matches"][0]["enbez"] == "§ 24"
    assert {row["enbez"] for row in result["norm_matches"]} == {"§ 24"}
    hit = result["norm_matches"][0]
    assert hit["source"] == "gii"
    assert hit["url"] == "/acts/fed_aufenthg_2004"
    assert {"enbez", "norm_title", "text"} <= set(hit["matched_fields"])


@pytest.mark.parametrize(
    "query",
    ["SGB II § 74", "SGB 2 §74", "sgb-ii, §74", "§ 74 SGB II"],
)
def test_act_abbreviation_constrains_exact_norm_reference(
        engine: SearchEngine, query: str) -> None:
    result = engine.search(query, act_limit=10, norm_limit=10)

    assert result["norm_total"] == 1
    assert [(row["jurabk"], row["enbez"])
            for row in result["norm_matches"]] == [("SGB 2", "§ 74")]


@pytest.mark.parametrize(
    ("query", "jurabk", "enbez"),
    [("SGB III §74", "SGB 3", "§ 74"),
     ("SGB XII §146", "SGB 12", "§ 146"),
     ("AufenthG §24", "AufenthG 2004", "§ 24")],
)
def test_act_alias_variants_resolve_without_prefix_collisions(
        engine: SearchEngine, query: str, jurabk: str, enbez: str) -> None:
    result = engine.search(query, act_limit=10, norm_limit=10)

    assert result["norm_total"] == 1
    assert [(row["jurabk"], row["enbez"])
            for row in result["norm_matches"]] == [(jurabk, enbez)]


def test_legacy_matches_keep_plain_wiki_shape(engine: SearchEngine) -> None:
    result = engine.search("Ukraine", act_limit=10, norm_limit=10)

    assert result["total"] == result["act_total"] == 2
    assert all("score" not in row and "snippet" not in row
               for row in result["matches"])
    assert all("score" in row and "snippet" in row
               for row in result["act_matches"])
