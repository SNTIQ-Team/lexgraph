from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from official_transition_review import (  # noqa: E402
    article_in_scope,
    article_scope_is_ambiguous,
    command_matches,
    effective_date_for,
    review_transitions,
)


def test_article_scope_respects_absatz_exceptions() -> None:
    clause = ("Artikel 6 Nummer 1 und 14 sowie Artikel 15 Absatz 15 und 16")
    assert article_in_scope("6", clause)
    assert article_in_scope("15 Abs. 15", clause)
    assert not article_in_scope("15 Abs. 12", clause)
    assert not article_in_scope("7", clause)
    assert article_in_scope("4", "Artikel 2 bis 7")
    assert article_scope_is_ambiguous(
        "1", "Artikel 1 Nummer 1 Buchstabe c")
    assert not article_scope_is_ambiguous(
        "15 Abs. 12", "Artikel 15 Absatz 12")


def test_effective_date_uses_default_only_outside_exceptions() -> None:
    rows = [
        {"datum": "2026-07-10"},
        {"datum": "2027-01-01",
         "erlaeuterung": "Artikel 6 Nummer 1 und 14 sowie Artikel 15 Absatz 15 und 16"},
        {"datum": "2027-02-01",
         "erlaeuterung": "Artikel 1 Nummer 1 Buchstabe c"},
    ]
    assert effective_date_for("7", rows) == "2026-07-10"
    assert effective_date_for("15 Abs. 15", rows) == "2027-01-01"
    assert effective_date_for("15 Abs. 12", rows) == "2026-07-10"


def test_effective_date_rejects_narrower_exception_than_gii_reference() -> None:
    rows = [
        {"datum": "2026-07-10"},
        {"datum": "2027-02-01",
         "erlaeuterung": "Artikel 1 Nummer 1 Buchstabe c"},
    ]
    assert effective_date_for("1", rows) is None


def test_final_command_must_name_norm_and_contain_inserted_wording() -> None:
    change = {
        "para": "§ 29a", "operation": "replace",
        "old": "(2a) Der alte Bericht wird alle zwei Jahre vorgelegt.",
        "new": ("(2a) Die Bundesregierung legt dem Deutschen Bundestag alle "
                "zwei Jahre einen Bericht über sichere Herkunftsstaaten vor."),
    }
    command = ("§ 29a Absatz 2a wird durch den folgenden Absatz ersetzt: "
               "(2a) Die Bundesregierung legt dem Deutschen Bundestag alle "
               "zwei Jahre einen Bericht über sichere Herkunftsstaaten vor.")
    assert command_matches(change, command)
    assert not command_matches(change, command.replace("§ 29a", "§ 30"))
    assert not command_matches(change, "§ 29a Absatz 2a wird geändert.")


def test_review_requires_all_three_official_gates() -> None:
    old = "(2a) Der alte Bericht wird alle zwei Jahre vorgelegt."
    new = ("(2a) Die Bundesregierung legt dem Deutschen Bundestag alle zwei "
           "Jahre einen Bericht über sichere Herkunftsstaaten vor.")
    transition = {
        "act_id": "fed_asylvfg_1992", "jurabk": "AsylVfG 1992",
        "observed_at": "2026-07-13", "previous_observed_at": "2026-07-06",
        "state_sha256": "b" * 64, "previous_state_sha256": "a" * 64,
        "old_builddate": "20260611", "new_builddate": "20260709",
        "full_state_pair": True,
        "source_url": "https://www.gesetze-im-internet.de/asylvfg_1992/",
        "changes": [{"para": "§ 29a", "operation": "replace",
                     "old": old, "new": new,
                     "old_present": True, "new_present": True}],
    }
    document = {
        "document_id": "bgbl-1-2026-199", "year": "2026", "issue": "199",
        "procedure_id": "327966", "publication_date": "2026-07-09",
        "integrity_verified": True, "sha256": "c" * 64,
        "text_sha256": "d" * 64,
        "official_pdf_url": "https://www.recht.bund.de/x.pdf",
        "eli": "https://www.recht.bund.de/eli/bund/bgbl-1/2026/199",
        "dip_entry_into_force": [{"datum": "2026-07-10"}],
        "referenced_corpus_acts": [{
            "jurabk": "AsylVfG 1992", "article": "7"}],
        "articles": [{
            "article": "7", "heading": "Änderung des Asylgesetzes",
            "text": f"Artikel 7\n§ 29a Absatz 2a wird ersetzt: {new}"}],
    }
    reviews = review_transitions([transition], [document])
    assert len(reviews) == 1
    review = reviews[0]
    assert review["published_at"] == "2026-07-09"
    assert review["effective_at"] == "2026-07-10"
    assert review["amending_articles"] == ["7"]
    assert review["verification"] == \
        "official_final_text_and_complete_state_pair"

    broken = {**document, "integrity_verified": False}
    assert review_transitions([transition], [broken]) == []
