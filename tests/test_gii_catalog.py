from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402
from api.gii_catalog import GiiCatalogIndex, search_gii_catalog  # noqa: E402
from fetch_gii import parse_toc  # noqa: E402


TOC = b"""<?xml version='1.0' encoding='UTF-8'?>
<items>
  <item>
    <title>  Buergerliches   Gesetzbuch  </title>
    <link>http://www.gesetze-im-internet.de/bgb/xml.zip</link>
  </item>
  <item>
    <title>Gesetz zu Deutschland und der Ukraine ueber Soziale Sicherheit</title>
    <link>https://www.gesetze-im-internet.de/sozsichabkg_ukr/xml.zip</link>
  </item>
  <item><title>Ignored malformed row</title><link>/no-zip-here</link></item>
</items>"""


def test_parse_toc_builds_stable_metadata_without_act_downloads() -> None:
    links, rows = parse_toc(TOC)

    assert links == {
        "bgb": "https://www.gesetze-im-internet.de/bgb/xml.zip",
        "sozsichabkg_ukr": (
            "https://www.gesetze-im-internet.de/sozsichabkg_ukr/xml.zip"),
    }
    assert rows == [{
        "id": "gii:bgb",
        "abbrev": "bgb",
        "title": "Buergerliches Gesetzbuch",
        "url": "https://www.gesetze-im-internet.de/bgb/",
    }, {
        "id": "gii:sozsichabkg_ukr",
        "abbrev": "sozsichabkg_ukr",
        "title": "Gesetz zu Deutschland und der Ukraine ueber Soziale Sicherheit",
        "url": "https://www.gesetze-im-internet.de/sozsichabkg_ukr/",
    }]


def test_build_catalog_enriches_only_curated_rows(
        tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "2026-07-15"
    snapshot.mkdir()
    _, catalog = parse_toc(TOC)
    (snapshot / "catalog.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in catalog),
        encoding="utf-8")
    (snapshot / "acts.jsonl").write_text(json.dumps({
        "slug": "bgb", "jurabk": "BGB",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(web_data, "latest_snapshot",
                        lambda source: snapshot if source == "gii" else None)

    result = web_data.build_gii_catalog([{
        "id": "fed_bgb", "jurabk": "BGB", "juris": "DE",
        "title": "Buergerliches Gesetzbuch",
    }])

    assert result is not None
    assert result["total"] == 2
    assert result["acts"][0]["act_id"] == "fed_bgb"
    assert result["acts"][0]["jurabk"] == "BGB"
    assert "act_id" not in result["acts"][1]
    assert "jurabk" not in result["acts"][1]


def test_catalog_ranking_normalizes_and_prefers_exact_abbreviation() -> None:
    rows = [{
        "id": "gii:bgb", "abbrev": "bgb", "jurabk": "BGB",
        "title": "Bürgerliches Gesetzbuch", "act_id": "fed_bgb",
        "url": "https://www.gesetze-im-internet.de/bgb/",
    }, {
        "id": "gii:bgbeg", "abbrev": "bgbeg",
        "title": "Einführungsgesetz zum Bürgerlichen Gesetzbuche",
        "url": "https://www.gesetze-im-internet.de/bgbeg/",
    }]

    hits, total = search_gii_catalog(rows, "BGB")
    assert total == 2
    assert [row["id"] for row in hits] == ["gii:bgb", "gii:bgbeg"]
    assert hits[0]["matched_fields"] == ["id", "abbrev", "jurabk"]
    assert all(row["source"] == "gii_catalog" for row in hits)

    hits, total = search_gii_catalog(rows, "burgerliches gesetz")
    assert total == 1
    assert hits[0]["id"] == "gii:bgb"


def test_catalog_index_supports_reviewed_common_title_aliases() -> None:
    rows = [{
        "id": "gii:burlg", "abbrev": "burlg",
        "title": "Mindesturlaubsgesetz für Arbeitnehmer",
        "url": "https://www.gesetze-im-internet.de/burlg/",
    }]
    index = GiiCatalogIndex(rows)

    hits, total = index.search("Bundesurlaubsgesetz")

    assert total == 1
    assert hits[0]["id"] == "gii:burlg"
    assert "alias" in hits[0]["matched_fields"]


def test_api_search_appends_only_non_deep_catalog_matches(
        tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from api import main as api_main

    wiki = [{
        "id": "fed_ukraineaufenth_v", "jurabk": "UkraineAufenthUEV",
        "juris": "DE", "title": "Ukraine Aufenthalt Verordnung",
    }]
    catalog = {"acts": [{
        "id": "gii:ukraineaufenth_v", "abbrev": "ukraineaufenth_v",
        "title": "Ukraine Aufenthalt Verordnung",
        "url": "https://www.gesetze-im-internet.de/ukraineaufenth_v/",
        "act_id": "fed_ukraineaufenth_v", "jurabk": "UkraineAufenthUEV",
    }, {
        "id": "gii:sozsichabkg_ukr", "abbrev": "sozsichabkg_ukr",
        "title": "Abkommen mit der Ukraine ueber Soziale Sicherheit",
        "url": "https://www.gesetze-im-internet.de/sozsichabkg_ukr/",
    }]}

    def fake_load(name: str):
        return {"wiki": wiki, "hierarchy": {},
                "gii_catalog": catalog}[name]

    monkeypatch.setattr(api_main, "_load", fake_load)
    monkeypatch.setattr(api_main, "DATA_DIR", tmp_path)
    result = api_main.search("Ukraine", limit=25, norm_limit=50,
                             procedure_limit=20, catalog_limit=25)

    assert result["act_total"] == 1
    assert result["catalog_total"] == 1
    assert result["result_total"] == 2
    assert [row["id"] for row in result["catalog_matches"]] == [
        "gii:sozsichabkg_ukr"]
    assert "act_id" not in result["catalog_matches"][0]
