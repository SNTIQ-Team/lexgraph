from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from fetch_bgbl_documents import (  # noqa: E402
    SourceIntegrityError,
    canonical_urls,
    cumulative_documents,
    parse_article_sections,
    parse_gii_references,
    parse_vo_html,
    select_documents,
    verify_advertised_md5,
)


def _official_html(md5: str) -> str:
    return f"""
    <!doctype html><html><head>
      <link rel="canonical"
            href="https://www.recht.bund.de/bgbl/1/2026/112/VO.html">
    </head><body>
      <a href="https://www.recht.bund.de/eli/bund/bgbl-1/2026/112">ELI</a>
      <h1 id="introH"><span>Gesetz zur Änderung des AZR-Gesetzes</span>
        (AZR-Änderungsgesetz)</h1>
      <div role="listitem"><strong>BGBl.-Nr.:</strong><span>112</span></div>
      <div role="listitem"><strong>Veröffentlichungsdatum:</strong>
        <span>28.04.2026</span></div>
      <div role="listitem"><strong>Ausfertigungsdatum:</strong>
        <span>23.04.2026</span></div>
      <a href="/bgbl/1/2026/112/regelungstext.pdf?__blob=publicationFile&amp;v=1">
        Regelungstext</a>
      <span class="c-tooltip__content--hash">{md5}</span>
    </body></html>
    """


def test_canonical_urls_reject_path_injection() -> None:
    assert canonical_urls("2026", "00112") == {
        "eli": "https://www.recht.bund.de/eli/bund/bgbl-1/2026/112",
        "html": "https://www.recht.bund.de/bgbl/1/2026/112/VO.html",
        "pdf": ("https://www.recht.bund.de/bgbl/1/2026/112/"
                "regelungstext.pdf?__blob=publicationFile&v=1"),
    }
    with pytest.raises(ValueError):
        canonical_urls("2026", "112/../../x")


def test_parse_official_html_metadata_and_hash() -> None:
    parsed = parse_vo_html(_official_html("a" * 32),
                           "https://www.recht.bund.de/fallback")
    assert parsed["title"] == (
        "Gesetz zur Änderung des AZR-Gesetzes (AZR-Änderungsgesetz)")
    assert parsed["canonical_url"].endswith("/2026/112/VO.html")
    assert parsed["pdf_url"] == (
        "https://www.recht.bund.de/bgbl/1/2026/112/regelungstext.pdf"
        "?__blob=publicationFile&v=1")
    assert parsed["eli"].endswith("/eli/bund/bgbl-1/2026/112")
    assert parsed["advertised_md5"] == "a" * 32
    assert parsed["publication_date"] == "2026-04-28"
    assert parsed["execution_date"] == "2026-04-23"


def test_hash_mismatch_fails_closed() -> None:
    payload = b"%PDF-1.4\nfixture"
    advertised = hashlib.md5(b"different").hexdigest()
    with pytest.raises(SourceIntegrityError, match="MD5 mismatch"):
        verify_advertised_md5(payload, advertised)
    assert verify_advertised_md5(
        payload, hashlib.md5(payload).hexdigest()) == hashlib.md5(
            payload).hexdigest()


def test_parse_articles_7_and_16_without_matching_inline_references() -> None:
    text = """Gesetz über einen Test

Artikel 7
Änderung des Testgesetzes
Nach Artikel 16 der Verordnung (EU) 2024/1 gilt etwas anderes.
§ 1 wird geändert.

Artikel 16
Inkrafttreten
(1) Dieses Gesetz tritt am 1. Januar 2027 in Kraft.
(2) Artikel 7 tritt am Tag nach der Verkündung in Kraft.
Berlin, den 1. Dezember 2026
"""
    sections, entry = parse_article_sections(text)
    assert [section["article"] for section in sections] == ["7", "16"]
    assert sections[0]["heading"] == "Änderung des Testgesetzes"
    assert "Artikel 16 der Verordnung" in sections[0]["text"]
    assert entry is not None
    assert entry["article"] == "16"
    assert entry["heading"] == "Inkrafttreten"
    assert entry["effective_dates_inferred"] is False
    assert "1. Januar 2027" in entry["text"]


def test_gii_reference_parser_keeps_act_and_amending_article() -> None:
    refs = parse_gii_references([{
        "slug": "asylblg", "jurabk": "AsylbLG",
        "stand": ("Neufassung; zuletzt geändert durch Art. 3 G v. "
                  "23.4.2026 I Nr. 112"),
    }, {
        "slug": "azrg", "jurabk": "AZRG",
        "stand": "Zuletzt geändert durch Art. 11 Abs. 17 G v. 23.4.2026 I Nr. 112",
    }])
    rows = refs[("2026", "112")]
    assert [(row["jurabk"], row["article"]) for row in rows] == [
        ("AsylbLG", "3"), ("AZRG", "11 Abs. 17")]
    assert all(row["amendment_date"] == "2026-04-23" for row in rows)


def test_select_documents_requires_gii_reference_and_dip_promulgation() -> None:
    acts = [{
        "slug": "asylblg", "jurabk": "AsylbLG",
        "stand": "zuletzt geändert durch Art. 3 G v. 23.4.2026 I Nr. 112",
    }]
    procedures = [{
        "id": "327664", "titel": "GEAS-Anpassungsfolgegesetz",
        "beratungsstand": "Verkündet", "aktualisiert": "2026-04-30T12:00:00+02:00",
        "verkuendung": [{
            "jahrgang": "2026", "heftnummer": "112",
            "ausfertigungsdatum": "2026-04-23",
            "verkuendungsdatum": "2026-04-28",
            "pdf_url": "https://www.recht.bund.de/eli/bund/BGBl_1/2026/112",
        }],
        "inkrafttreten": [{"datum": "2026-06-12", "erlaeuterung": "Artikel 1"}],
    }, {
        "id": "ignored", "titel": "Nicht im Korpus referenziert",
        "verkuendung": [{"jahrgang": "2026", "heftnummer": "999"}],
    }]
    rows = select_documents(acts, procedures)
    assert len(rows) == 1
    row = rows[0]
    assert row["document_id"] == "bgbl-1-2026-112"
    assert row["procedure_id"] == "327664"
    assert row["publication_date"] == "2026-04-28"
    assert row["execution_date"] == "2026-04-23"
    assert row["dip_entry_into_force"][0]["datum"] == "2026-06-12"
    assert row["referenced_corpus_acts"][0]["article"] == "3"


def test_bgbl_metadata_snapshot_is_cumulative() -> None:
    old = {
        "document_id": "bgbl-1-2025-10", "year": "2025", "issue": "10",
        "publication_date": "2025-01-20", "sha256": "a" * 64,
    }
    previous_current = {
        "document_id": "bgbl-1-2026-112", "year": "2026", "issue": "112",
        "publication_date": "2026-04-28", "procedure_status": "old",
    }
    refreshed_current = {**previous_current, "procedure_status": "Verkündet"}
    rows = cumulative_documents({
        old["document_id"]: old,
        previous_current["document_id"]: previous_current,
    }, [refreshed_current])
    assert [row["document_id"] for row in rows] == [
        "bgbl-1-2026-112", "bgbl-1-2025-10"]
    assert rows[0]["procedure_status"] == "Verkündet"
