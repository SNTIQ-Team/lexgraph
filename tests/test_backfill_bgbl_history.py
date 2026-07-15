from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from backfill_bgbl_history import (  # noqa: E402
    act_aliases,
    article_scope_kinds,
    build_inventory,
    descriptor_matches,
    exact_final_text_match,
    legal_name_forms,
    parse_retrospective_article_sections,
    parse_amendment_commands,
    resolve_article_effective_date,
    select_document_candidates,
)


def _act(slug: str = "aufenthg_2004") -> dict:
    return {
        "slug": slug,
        "jurabk": "AufenthG 2004",
        "long_title": (
            "Gesetz über den Aufenthalt, die Erwerbstätigkeit und die "
            "Integration von Ausländern im Bundesgebiet"
        ),
    }


def test_short_title_aliases_are_exact_not_abbreviation_fuzzy() -> None:
    aliases = act_aliases(_act())
    assert "aufenthaltsgesetz" in aliases
    assert "aufenthg 2004" not in aliases


def test_descriptor_match_requires_exact_legal_material_name() -> None:
    procedure = {
        "deskriptor": [
            {"typ": "Rechtsmaterialien", "name": "Aufenthaltsgesetz"},
            {"typ": "Sachbegriffe", "name": "Aufenthalt"},
        ]
    }
    rows = descriptor_matches(procedure, [_act()])
    assert len(rows) == 1
    assert rows[0]["descriptor_aliases"] == ["aufenthaltsgesetz"]
    assert descriptor_matches({"deskriptor": [
        {"typ": "Rechtsmaterialien", "name": "Aufenthaltsrecht"},
    ]}, [_act()]) == []


def test_final_text_match_requires_name_in_heading_or_preamble() -> None:
    matched = descriptor_matches({"deskriptor": [
        {"typ": "Rechtsmaterialien", "name": "Aufenthaltsgesetz"},
    ]}, [_act()])[0]
    section = {
        "heading": "Änderung des Aufenthaltsgesetzes",
        "text": ("Artikel 2\nÄnderung des Aufenthaltsgesetzes\n"
                 "Das Aufenthaltsgesetz wird wie folgt geändert."),
    }
    assert exact_final_text_match(section, matched) == "aufenthaltsgesetz"
    assert exact_final_text_match({
        "heading": "Sonstige Änderungen",
        "text": "Artikel 2\nEine aufenthaltsrechtliche Vorschrift wird geprüft.",
    }, matched) is None


def test_final_text_match_accepts_exact_inflected_legal_names() -> None:
    assert "bürgerliche gesetzbuch" in legal_name_forms(
        "Bürgerliches Gesetzbuch")
    assert "bürgerlichen gesetzbuchs" in legal_name_forms(
        "Bürgerliches Gesetzbuch")
    assert "arbeitszeitgesetzes" in legal_name_forms("Arbeitszeitgesetz")
    assert "fünften buches sozialgesetzbuch" in legal_name_forms(
        "Fünftes Buch Sozialgesetzbuch")

    bgb = {
        "aliases": ["bürgerliches gesetzbuch"],
        "slug": "bgb",
        "jurabk": "BGB",
        "long_title": "Bürgerliches Gesetzbuch",
    }
    section = {
        "heading": "Änderung des Bürgerlichen Gesetzbuchs",
        "text": ("Artikel 1 Änderung des Bürgerlichen Gesetzbuchs "
                 "Das Bürgerliche Gesetzbuch wird wie folgt geändert."),
    }
    assert exact_final_text_match(section, bgb) == "bürgerliches gesetzbuch"


def test_final_text_match_rejects_name_only_in_replacement_text() -> None:
    asylblg = {
        "aliases": ["asylbewerberleistungsgesetz"],
        "slug": "asylblg",
        "jurabk": "AsylbLG",
        "long_title": "Asylbewerberleistungsgesetz",
    }
    sgb_section = {
        "heading": "Änderung des Fünften Buches Sozialgesetzbuch",
        "text": (
            "Artikel 4 Änderung des Fünften Buches Sozialgesetzbuch "
            "Das Fünfte Buch Sozialgesetzbuch wird wie folgt geändert: "
            "In § 264 werden die Wörter ‚nach dem Asylbewerberleistungsgesetz‘ "
            "durch andere Wörter ersetzt."
        ),
    }
    assert exact_final_text_match(sgb_section, asylblg) is None


def test_final_text_match_rejects_nested_replacement_article_heading() -> None:
    gg = {
        "aliases": ["grundgesetz"],
        "slug": "gg",
        "jurabk": "GG",
        "long_title": "Grundgesetz für die Bundesrepublik Deutschland",
    }
    nested = {
        "heading": "(1) Das Bundesverfassungsgericht entscheidet:",
        "text": ("Artikel 94 (1) Das Bundesverfassungsgericht entscheidet "
                 "über die Auslegung dieses Grundgesetzes."),
    }
    assert exact_final_text_match(nested, gg) is None


def test_final_text_match_rejects_generic_new_law_article() -> None:
    bgb = {
        "aliases": ["bürgerliches gesetzbuch"],
        "slug": "bgb",
        "jurabk": "BGB",
        "long_title": "Bürgerliches Gesetzbuch",
    }
    new_law = {
        "heading": "Gesetz",
        "text": (
            "Artikel 1 Gesetz über einen neuen Gegenstand. "
            "§ 30 verweist auf das Bürgerliche Gesetzbuch. "
            "Die Angabe wird später geändert."
        ),
    }
    assert exact_final_text_match(new_law, bgb) is None


def test_effective_date_resolution_is_fail_closed_for_sub_article_scope() -> None:
    rows = [
        {"datum": "2026-07-10"},
        {"datum": "2027-02-01",
         "erlaeuterung": "Artikel 1 Nr. 1 Buchst. c, Nr. 4 und 5"},
    ]
    result = resolve_article_effective_date("1", rows)
    assert result["effective_at"] is None
    assert result["status"] == "unresolved_sub_article_scope"


def test_effective_date_resolution_supports_exact_and_remainder_clauses() -> None:
    exact = resolve_article_effective_date("7", [
        {"datum": "2026-09-01", "erlaeuterung": "im Übrigen"},
        {"datum": "2027-01-01", "erlaeuterung": "Artikel 7"},
    ])
    assert exact["effective_at"] == "2027-01-01"
    assert exact["status"] == "resolved_explicit_article_clause"

    remainder = resolve_article_effective_date("8", [
        {"datum": "2026-09-01", "erlaeuterung": "im Übrigen"},
        {"datum": "2027-01-01", "erlaeuterung": "Artikel 7"},
    ])
    assert remainder["effective_at"] == "2026-09-01"
    assert remainder["status"] == "resolved_remainder_clause"


def test_dip_article_lists_resolve_whole_and_narrowed_members() -> None:
    explanation = "Artikel 1, 2 und 3 Nr. 3 bis 6, die Artikel 4 bis 6"
    assert article_scope_kinds("1", explanation) == {"whole"}
    assert article_scope_kinds("2", explanation) == {"whole"}
    assert article_scope_kinds("3", explanation) == {"narrowed"}
    assert article_scope_kinds("4", explanation) == {"whole"}
    assert article_scope_kinds("6", explanation) == {"whole"}

    rows = [
        {"datum": "2026-07-10", "erlaeuterung": "im Übrigen"},
        {"datum": "2027-01-01", "erlaeuterung": explanation},
    ]
    assert resolve_article_effective_date("2", rows)["effective_at"] == \
        "2027-01-01"
    assert resolve_article_effective_date("3", rows)["effective_at"] is None


def test_dip_shorthand_narrowed_article_is_not_treated_as_default() -> None:
    explanation = "Artikel 12, 13 Nr. 4, 13a Nr. 2, Artikel 14"
    assert article_scope_kinds("12", explanation) == {"whole"}
    assert article_scope_kinds("13", explanation) == {"narrowed"}
    assert article_scope_kinds("13a", explanation) == {"narrowed"}
    assert article_scope_kinds("14", explanation) == {"whole"}


def test_retrospective_splitter_and_scope_support_8z_article_ids() -> None:
    text = (
        "Artikel 8z\nÄnderung des Gesetzes A\nA\n"
        "Artikel 8z1\nÄnderung des Gesetzes B\nB\n"
        "Artikel 8z2\nInkrafttreten\nC\n"
        "Artikel 9\nWeitere Änderung\nD\n"
    )
    sections, entry = parse_retrospective_article_sections(text)
    assert [row["article"] for row in sections] == ["8z", "8z1", "8z2", "9"]
    assert entry and entry["article"] == "8z2"
    assert article_scope_kinds(
        "8z1", "Artikel 2a, 3a, 8z1, 8z2 und 8z3") == {"whole"}
    assert article_scope_kinds(
        "13b", "Artikel 2, 13 b Nr. 1 und Artikel 14") == {"narrowed"}


def test_select_documents_enforces_bgbl_part_and_publication_range() -> None:
    act = _act()
    procedure = {
        "id": "123",
        "wahlperiode": 20,
        "titel": "Änderungsgesetz",
        "beratungsstand": "Verkündet",
        "aktualisiert": "2024-02-03T00:00:00+01:00",
        "deskriptor": [
            {"typ": "Rechtsmaterialien", "name": "Aufenthaltsgesetz"},
        ],
        "inkrafttreten": [{"datum": "2024-02-02"}],
        "verkuendung": [
            {"jahrgang": "2024", "heftnummer": "23",
             "verkuendungsdatum": "2024-02-01",
             "ausfertigungsdatum": "2024-01-30",
             "verkuendungsblatt_kuerzel": "BGBl I"},
            {"jahrgang": "2024", "heftnummer": "24",
             "verkuendungsdatum": "2024-02-01",
             "verkuendungsblatt_kuerzel": "BGBl II"},
            {"jahrgang": "2022", "heftnummer": "1",
             "verkuendungsdatum": "2022-01-01",
             "verkuendungsblatt_kuerzel": "BGBl I"},
        ],
    }
    rows = select_document_candidates(
        [procedure], [dict(act, aliases=list(act_aliases(act)))],
        "2023-01-01", "2026-12-31")
    assert [row["document_id"] for row in rows] == ["bgbl-1-2024-23"]


def test_inventory_keeps_dates_and_does_not_claim_reconstruction() -> None:
    act = _act()
    matched = descriptor_matches({"deskriptor": [
        {"typ": "Rechtsmaterialien", "name": "Aufenthaltsgesetz"},
    ]}, [act])[0]
    document = {
        "document_id": "bgbl-1-2024-23",
        "integrity_verified": True,
        "sha256": "a" * 64,
        "md5": "b" * 32,
        "advertised_md5": "b" * 32,
        "text_sha256": "c" * 64,
        "execution_date": "2024-01-30",
        "publication_date": "2024-02-01",
        "eli": "https://www.recht.bund.de/eli/bund/bgbl-1/2024/23",
        "official_html_url": "https://www.recht.bund.de/bgbl/1/2024/23/VO.html",
        "official_pdf_url": "https://www.recht.bund.de/example.pdf",
        "pdf_object": "data/bgbl_documents/objects/a.pdf",
        "text_object": "data/bgbl_documents/texts/c.txt",
        "articles": [{
            "article": "2",
            "heading": "Änderung des Aufenthaltsgesetzes",
            "text": ("Artikel 2\nÄnderung des Aufenthaltsgesetzes\n"
                     "Das Aufenthaltsgesetz wird wie folgt geändert."),
        }],
        "retrospective_procedures": [{
            "procedure_id": "123",
            "wahlperiode": 20,
            "procedure_title": "Änderungsgesetz",
            "dip_entry_into_force": [{"datum": "2024-02-02"}],
            "matched_acts": [matched],
        }],
    }
    rows = build_inventory([document])
    assert len(rows) == 1
    assert rows[0]["execution_date"] == "2024-01-30"
    assert rows[0]["publication_date"] == "2024-02-01"
    assert rows[0]["effective_at"] == "2024-02-02"
    assert rows[0]["historical_text_reconstructed"] is False
    assert rows[0]["candidate_only"] is True


def test_amendment_commands_keep_norms_not_outer_article_number() -> None:
    commands, norms = parse_amendment_commands({
        "article": "4",
        "heading": "Änderung des Grundgesetzes",
        "text": (
            "Artikel 4\nÄnderung des Grundgesetzes\n"
            "Das Grundgesetz wird wie folgt geändert:\n"
            "1. Artikel 16a Absatz 2 wird wie folgt gefasst: ‚…‘\n"
            "2. In § 24 Absatz 1 werden die Wörter ‚alt‘ durch ‚neu‘ ersetzt."
        ),
    })
    assert norms == ["§ 24", "Art. 16a"]
    assert commands[0]["ref"]["article"] == "16a"
    assert all(row.get("ref", {}).get("article") != "4" for row in commands)
