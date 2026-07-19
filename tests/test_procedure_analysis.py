from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from procedure_analysis import analyse_procedure  # noqa: E402


def test_active_dip_analysis_uses_positions_and_operative_text_check() -> None:
    row = {
        "id": "329468", "source": "DIP", "status": "Überwiesen",
        "stage": "Überwiesen", "tracking_state": "active",
        "url": "https://dip.example/329468",
        "abstract": "Bezug: Vereinbarung im Koalitionsvertrag für eingereiste Flüchtlinge",
        "initiators": ["Bundesregierung"],
        "approval_requirements": ["Ja, laut Gesetzentwurf"],
        "positions": [
            {"id": "1", "date": "2026-01-15", "stage": "1. Beratung",
             "chamber": "BT", "document": {"number": "21/53",
                                                "url": "https://bt.example/53"},
             "content_validations": []},
            {"id": "2", "date": "2026-02-23",
             "stage": "Öffentliche Anhörung", "chamber": "BT",
             "document": {"number": "bundestag-hearing",
                          "url": "https://bt.example/hearing"},
             "content_validations": [{
                 "id": "hearing", "kind": "official_event",
                 "label": "Hearing", "finding": "Anhörung durchgeführt",
                 "passed": True, "retrieval_status": "fetched",
                 "source_url": "https://bt.example/hearing",
             }]},
            {"id": "3", "date": "2026-01-12", "stage": "Gesetzentwurf",
             "chamber": "BT", "document": {"number": "21/3539",
                                                "url": "https://bt.example/draft"},
             "content_validations": [{
                 "id": "ukraine-cutoff-operative-text",
                 "kind": "operative_text", "label": "Cutoff",
                 "finding": "eAT/Fiktionsbescheinigung ist maßgeblich",
                 "passed": True, "retrieval_status": "fetched",
                 "source_url": "https://bt.example/draft",
             }]},
            {"id": "4", "date": "2026-01-30", "stage": "Empfehlungen",
             "chamber": "BR", "abstract": "Stellungnahme: Änderungen",
             "document": {"number": "763/25(B)",
                          "url": "https://br.example/763"},
             "content_validations": []},
        ],
    }
    config = {
        "id": "ukraine-rechtskreiswechsel", "draft_only": True,
        "scope_source": "https://bt.example/draft",
    }

    analysis = analyse_procedure(
        row, config, [], [], "2026-07-15T08:17:00Z")

    assert analysis["forecast"]["outcome"] == \
        "progress_toward_committee_recommendation_likely"
    assert analysis["forecast"]["likelihood"]["band"] == "moderate"
    assert analysis["forecast"]["likelihood"]["minimum"] is None
    assert analysis["forecast"]["confidence"] == "medium_low"
    assert {factor["label"] for factor in analysis["factors"]} >= {
        "Initiative der Bundesregierung",
        "Öffentliche Anhörung durchgeführt",
        "Änderungsforderungen des Bundesrates",
        "Noch keine Beschlussempfehlung nach der Anhörung",
        "142 Tage ohne neue DIP-Verfahrensposition",
    }
    checks = {check["id"]: check for check in analysis["checks"]}
    assert checks["ukraine-cutoff-operative-text"]["status"] == "passed"
    assert checks["dip_summary_not_operative_rule"]["status"] == "passed"
    assert any(event["label"] == "Öffentliche Anhörung"
               for event in analysis["chronology"])


def test_eu_preparation_is_not_upgraded_to_political_agreement() -> None:
    row = {
        "id": "eu-x", "source": "EUR-Lex", "status": "Ongoing",
        "stage": "Preparation for a political agreement",
        "tracking_state": "active", "adopted_celexes": [],
        "official_journal": [], "url": "https://eurlex.example/procedure",
    }
    config = {
        "celex_proposal": "52026PC0345",
        "proposal_url": "https://eurlex.example/proposal",
        "scope_source": "https://eurlex.example/proposal",
        "council_register_url": "https://consilium.example/register",
    }

    analysis = analyse_procedure(row, config, [], [], "2026-07-15T08:17:00Z")

    assert analysis["forecast"]["outcome"] == \
        "extension_likely_exact_article_2_uncertain"
    assert analysis["forecast"]["not_a_fact"] is True
    facts = {fact["id"] for fact in analysis["facts"]}
    assert "council_prepares_political_agreement" in facts
    assert "council_political_agreement" not in facts
    assert {check["id"]: check["status"] for check in analysis["checks"]}[
        "adopted_act_identified"] == "pending"
    assert any(item["id"] == "article_2_wording_uncertain"
               for item in analysis["inferences"])


def test_communicated_political_agreement_strengthens_forecast() -> None:
    row = {
        "id": "eu-x", "source": "EUR-Lex", "status": "Ongoing",
        "stage": "Political agreement — formal Council adoption pending",
        "tracking_state": "active", "adopted_celexes": [],
        "official_journal": [], "url": "https://eurlex.example/procedure",
        "council_communication": {
            "source": "Council press release",
            "date": "2026-07-15",
            "title": ("EU countries agree to extend temporary protection "
                      "for those fleeing Ukraine until March 2028"),
            "url": "https://consilium.example/press",
            "stage": "Political agreement — formal Council adoption pending",
            "retrieval_status": "verified_seed",
        },
    }
    config = {
        "celex_proposal": "52026PC0345",
        "proposal_url": "https://eurlex.example/proposal",
        "scope_source": "https://eurlex.example/proposal",
        "council_register_url": "https://consilium.example/register",
    }

    analysis = analyse_procedure(row, config, [], [], "2026-07-19T10:00:00Z")

    forecast = analysis["forecast"]
    assert forecast["outcome"] == "formal_adoption_and_publication_expected"
    assert forecast["likelihood"]["band"] == "very_high"
    assert forecast["not_a_fact"] is True
    # Practically agreed, but never presented as adopted law.
    facts = {fact["id"] for fact in analysis["facts"]}
    assert "council_political_agreement" in facts
    assert "council_prepares_political_agreement" not in facts
    checks = {check["id"]: check["status"] for check in analysis["checks"]}
    assert checks["adopted_act_identified"] == "pending"
    assert checks["official_journal_publication"] == "pending"
    conditions = analysis["next_milestone"]["conditions"]
    assert "förmliche Ratsannahme" in conditions
    assert "sprachjuristische Überarbeitung" in conditions
    assert "Amtsblattveröffentlichung" in conditions
    press_fact = next(fact for fact in analysis["facts"]
                      if fact["id"] == "council_political_agreement")
    assert "https://consilium.example/press" in press_fact["source_urls"]


def test_political_agreement_signal_retires_after_adoption_evidence() -> None:
    row = {
        "id": "eu-x", "source": "EUR-Lex", "status": "Ongoing",
        "stage": "Adoption by Council",
        "tracking_state": "active",
        "adopted_celexes": ["32026D1999"],
        "official_journal": [{"celex": "32026D1999",
                              "citation": "OJ L, 2026/1999"}],
        "url": "https://eurlex.example/procedure",
        "council_communication": {
            "source": "Council press release",
            "date": "2026-07-15",
            "title": "EU countries agree to extend temporary protection",
            "url": "https://consilium.example/press",
            "stage": "Political agreement — formal Council adoption pending",
            "retrieval_status": "verified_seed",
        },
    }
    config = {
        "celex_proposal": "52026PC0345",
        "proposal_url": "https://eurlex.example/proposal",
        "scope_source": "https://eurlex.example/proposal",
        "council_register_url": "https://consilium.example/register",
    }

    analysis = analyse_procedure(row, config, [], [], "2026-08-05T10:00:00Z")

    # Once adoption/publication evidence exists, the communicated agreement
    # must stop asserting that formal adoption is still outstanding.
    facts = {fact["id"] for fact in analysis["facts"]}
    assert "council_political_agreement" not in facts
    assert all("stehen noch aus" not in fact["statement"]
               for fact in analysis["facts"])
    assert analysis["forecast"]["outcome"] == "final_text_review_pending"


def test_chronology_lists_each_council_evidence_once() -> None:
    press_url = "https://consilium.example/press"
    register_url = "https://consilium.example/register"
    row = {
        "id": "eu-x", "source": "EUR-Lex", "status": "Ongoing",
        "stage": "Political agreement — formal Council adoption pending",
        "tracking_state": "active", "adopted_celexes": [],
        "official_journal": [], "url": "https://eurlex.example/procedure",
        "events": [
            {"date": "2026-07-10",
             "title": "Preparation for a political agreement",
             "source": "Council public register", "document": "ST 11375/26",
             "url": register_url},
            {"date": "2026-07-15",
             "title": "Political agreement — formal Council adoption pending",
             "headline": "EU countries agree to extend temporary protection",
             "source": "Council press release", "url": press_url},
        ],
        "council_development": {
            "source": "Council public register", "document": "ST 11375/26",
            "date": "2026-07-10", "title": "Long register title - Preparation",
            "stage": "Preparation for a political agreement",
            "url": register_url,
        },
        "council_communication": {
            "source": "Council press release", "date": "2026-07-15",
            "title": "EU countries agree to extend temporary protection",
            "stage": "Political agreement — formal Council adoption pending",
            "url": press_url,
        },
    }
    config = {"council_register_url": register_url}

    analysis = analyse_procedure(row, config, [], [], "2026-07-19T10:00:00Z")

    press_entries = [entry for entry in analysis["chronology"]
                     if press_url in entry["source_urls"]]
    register_entries = [entry for entry in analysis["chronology"]
                        if register_url in entry["source_urls"]]
    assert len(press_entries) == 1
    assert len(register_entries) == 1


def test_retrospective_result_requires_roles_transitions_and_current_law() -> None:
    row = {
        "id": "322125", "source": "DIP", "status": "Verkündet",
        "stage": "Verkündet", "terminal": True, "tracking_state": "terminal",
        "url": "https://dip.example/322125",
    }
    config = {"validation_ids": ["fate-x"]}
    roles = [
        ("government_draft", "introduced"),
        ("bundesrat_recommendation", "recommended_not_adopted"),
        ("committee_recommendation", "omitted"),
        ("plenary_resolution", "adopted_in_committee_version"),
        ("promulgated_law", "promulgated_without_proposal"),
    ]
    fate = {
        "id": "fate-x", "conclusion": "Die Änderung wurde nicht Gesetz.",
        "document_chain": [
            {"role": role, "disposition": disposition,
             "document": role, "date": f"2025-10-{index + 1:02d}",
             "finding": disposition, "url": f"https://official.example/{index}"}
            for index, (role, disposition) in enumerate(roles)
        ],
        "validation": {"passed": True},
        "current_sources": [{"url": "https://law.example/current"}],
    }

    analysis = analyse_procedure(
        row, config, [], [fate], "2026-07-15T08:17:00Z")

    assert analysis["forecast"]["outcome"] == \
        "retrospective_validation_complete"
    assert all(check["status"] == "passed" for check in analysis["checks"]
               if check["kind"] in {"chain", "transition", "final_text",
                                     "current_law"})
    assert len([event for event in analysis["chronology"]
                if event["kind"] == "document_chain"]) == 5
