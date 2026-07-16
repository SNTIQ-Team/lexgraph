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
    scope_section_to_act,
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


def test_specific_heading_rejects_statute_shaped_replacement_text() -> None:
    vwgo = {
        "aliases": ["verwaltungsgerichtsordnung"],
        "slug": "vwgo",
        "jurabk": "VwGO",
        "long_title": "Verwaltungsgerichtsordnung",
    }
    section = {
        "heading": "Änderung des Asylgesetzes",
        "text": (
            "Artikel 1\nÄnderung des Asylgesetzes\n"
            "Das Asylgesetz wird wie folgt geändert:\n"
            "1. § 10 wird wie folgt gefasst:\n"
            "„Die Verwaltungsgerichtsordnung wird wie folgt geändert: …“"
        ),
    }
    assert exact_final_text_match(section, vwgo) is None

    zpo = {
        "aliases": ["zivilprozessordnung"],
        "slug": "zpo", "jurabk": "ZPO",
        "long_title": "Zivilprozessordnung",
    }
    quoted_old_wording = {
        "heading": "Änderung der Verwaltungsgerichtsordnung",
        "text": (
            "Artikel 19\nÄnderung der Verwaltungsgerichtsordnung\n"
            "In § 173 der Verwaltungsgerichtsordnung werden die Wörter "
            "„Buch 6 der Zivilprozessordnung ist nicht anzuwenden“ gestrichen."
        ),
    }
    assert exact_final_text_match(quoted_old_wording, zpo) is None


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


def test_collective_follow_up_commands_are_scoped_per_act() -> None:
    bafoeg = {
        "slug": "baf_g", "jurabk": "BAföG",
        "long_title": "Bundesausbildungsförderungsgesetz",
        "aliases": ["bundesausbildungsförderungsgesetz"],
        "descriptor_aliases": ["bundesausbildungsförderungsgesetz"],
    }
    aufenthg = {
        "slug": "aufenthg_2004", "jurabk": "AufenthG 2004",
        "long_title": "Aufenthaltsgesetz",
        "aliases": ["aufenthaltsgesetz"],
        "descriptor_aliases": ["aufenthaltsgesetz"],
    }
    section = {
        "article": "11",
        "heading": "Folgeänderungen",
        "text": (
            "Artikel 11\nFolgeänderungen\n"
            "(1) Das Bundesausbildungsförderungsgesetz vom 1. Januar 2000 "
            "(BGBl. I S. 1) wird wie folgt geändert:\n"
            "In § 2 Absatz 1 wird die Angabe „alt“ durch die Angabe „neu“ "
            "ersetzt.\n"
            "(2) Das Aufenthaltsgesetz vom 1. Januar 2005 "
            "(BGBl. I S. 2) wird wie folgt geändert:\n"
            "1. § 44a Absatz 1 wird wie folgt geändert.\n"
            "2. § 104 Absatz 17 wird durch den folgenden Absatz ersetzt: "
            "„(17) Neu.“\n"
        ),
    }
    bafoeg_scope = scope_section_to_act(section, bafoeg)
    aufenthg_scope = scope_section_to_act(section, aufenthg)
    assert bafoeg_scope["collective_subsection"] == 1
    assert aufenthg_scope["collective_subsection"] == 2
    assert "§ 44a" not in bafoeg_scope["text"]
    assert "§ 2" not in aufenthg_scope["text"]

    document = {
        "document_id": "bgbl-1-2026-107",
        "integrity_verified": True,
        "sha256": "a" * 64,
        "md5": "b" * 32,
        "advertised_md5": "b" * 32,
        "text_sha256": "c" * 64,
        "execution_date": "2026-04-21",
        "publication_date": "2026-04-22",
        "articles": [section],
        "retrospective_procedures": [{
            "procedure_id": "321", "wahlperiode": 21,
            "procedure_title": "Änderungsgesetz",
            "dip_entry_into_force": [{"datum": "2026-07-01"}],
            "matched_acts": [bafoeg, aufenthg],
        }],
    }
    rows = {row["jurabk"]: row for row in build_inventory([document])}
    assert rows["BAföG"]["affected_norms"] == ["§ 2"]
    assert rows["BAföG"]["command_count"] == 1
    assert rows["AufenthG 2004"]["affected_norms"] == ["§ 44a", "§ 104"]
    assert rows["AufenthG 2004"]["command_count"] == 2
    assert rows["BAföG"]["command_scope_status"] == \
        "collective_subsection"


def test_numbered_leaf_commands_inherit_their_parent_norm() -> None:
    commands, norms = parse_amendment_commands({
        "article": "15",
        "heading": "Weitere Folgeänderungen",
        "text": (
            "(12) Das Erste Buch Sozialgesetzbuch (BGBl. I S. 3015) "
            "wird wie folgt geändert:\n"
            "§ 36a Absatz 2a wird wie folgt geändert:\n"
            "1. Nummer 2 Buchstabe d wird gestrichen.\n"
            "2. Nummer 3 wird durch die folgende Nummer 3 ersetzt: "
            "„3. neue Fassung.“"
        ),
    })
    assert norms == ["§ 36a"]
    assert len(commands) == 2
    assert all(command["ref"]["para"] == "36a" for command in commands)
    assert all(command["ref"]["absatz"] == "2a" for command in commands)
    assert commands[0]["ref"]["nummer"] == "2"
    assert commands[0]["parent_scope"].startswith("§ 36a Absatz 2a")


def test_commands_are_complete_hashed_and_free_of_pdf_page_headers() -> None:
    long_wording = "Amtlicher vollständiger Wortlaut " * 40
    commands, norms = parse_amendment_commands({
        "article": "3",
        "heading": "Änderung des Testgesetzes",
        "text": (
            "Artikel 3\nÄnderung des Testgesetzes\n"
            "Das Testgesetz wird wie folgt geändert:\n"
            "§ 7 wird wie folgt gefasst: „" + long_wording + "\n"
            "Seite 8 von 12 Bundesgesetzblatt Jahrgang 2026 Teil I Nr. 99, "
            "ausgegeben zu Bonn am 1. Juli 2026\n"
            "Schlusssatz.“"
        ),
    })
    assert norms == ["§ 7"]
    assert len(commands[0]["raw"]) > 800
    assert "Seite 8 von 12" not in commands[0]["raw"]
    assert len(commands[0]["raw_sha256"]) == 64
