from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.act_archive import (
    InvalidArchiveDateError,
    UnknownNormError,
    build_archive_index,
    head_date_for,
    markdown_filename,
    render_markdown_snapshot,
)


def _act(*, juris: str = "DE", norms: list[dict] | None = None,
         versions: list[dict] | None = None) -> dict:
    return {
        "id": "fed_testg" if juris == "DE" else "by_testg",
        "jurabk": "TestG",
        "juris": juris,
        "title": "Testgesetz",
        "build": "20200101",  # source fetch marker, not deployment HEAD
        "norms": norms or [
            {"enbez": "§ 1", "titel": "Erster Teil", "text": "Text eins"},
            {"enbez": "§ 24", "titel": "Schutz", "text": "Neuer Text"},
        ],
        "versions": versions or [],
    }


def test_head_markdown_is_exact_and_supports_full_act_or_one_norm() -> None:
    act = _act()
    assert head_date_for(act, "2026-07-15T12:34:56+00:00") == "2026-07-15"
    assert head_date_for(act, "2026-07-14T23:08:20+00:00") == "2026-07-15"

    full = render_markdown_snapshot(act, fallback_head="2026-07-15")
    assert full["exact"] is True
    assert full["partial"] is False
    assert full["resolved_at"] == "2026-07-15"
    assert "## § 1 — Erster Teil" in full["markdown"]
    assert "## § 24 — Schutz" in full["markdown"]

    one = render_markdown_snapshot(
        act, requested_at="2026-07-15", norm="§24",
        fallback_head="2026-07-15")
    assert one["norm"] == "§ 24"
    assert "Neuer Text" in one["markdown"]
    assert "Text eins" not in one["markdown"]
    assert markdown_filename(one) == "fed_testg-24-2026-07-15.md"


def test_effective_date_drives_reverse_state_not_publication_date() -> None:
    act = _act(juris="DE-BY", norms=[
        {"enbez": "Art. 44", "titel": "Befugnis", "text": "Neue Fassung"},
    ], versions=[{
        "date": "2024-07-23",
        "text": "Art. 44 geändert (GVBl. S. 247)",
        "changes": [{
            "para": "Art. 44",
            "effective_date": "2024-08-01",
            "old": "Alte Fassung",
            "new": "Neue Fassung",
        }],
    }])
    archive = build_archive_index(act, fallback_head="2026-07-15")
    dates = {entry["date"] for entry in archive["entries"]}
    assert {"2024-07-23", "2024-08-01", "2026-07-15"} <= dates

    before = render_markdown_snapshot(
        act, requested_at="2024-07-31", norm="Art.44",
        fallback_head="2026-07-15")
    assert "Alte Fassung" in before["markdown"]
    assert "Neue Fassung" not in before["markdown"]
    assert before["partial"] is True
    assert any(gap["reason"] == "reconstructed_not_source_snapshot"
               for gap in before["gaps"])

    effective = render_markdown_snapshot(
        act, requested_at="2024-08-01", norm="44",
        fallback_head="2026-07-15")
    assert "Neue Fassung" in effective["markdown"]


def test_wayback_state_anchor_is_used_when_later_head_no_longer_matches() -> None:
    act = _act(juris="DE-BY", norms=[
        {"enbez": "Art. 44", "titel": "Befugnis", "text": "Fassung 2026"},
    ], versions=[{
        "date": "2025-12-23",
        "text": "Spätere Änderung ohne Synopse",
    }, {
        "date": "2024-07-23",
        "text": "Art. 44 geändert (GVBl. S. 247)",
        "changes": [{
            "para": "Art. 44",
            "effective_date": "2024-08-01",
            "old_valid": "2023-05-07",
            "new_valid": "2024-08-01",
            "old": "Archivierte alte Fassung",
            "new": "Archivierte neue Fassung",
            "source": "wayback",
        }],
    }])

    before = render_markdown_snapshot(
        act, requested_at="2024-07-31", norm="Art. 44",
        fallback_head="2026-07-15")
    effective = render_markdown_snapshot(
        act, requested_at="2024-08-01", norm="Art. 44",
        fallback_head="2026-07-15")

    assert "Archivierte alte Fassung" in before["markdown"]
    assert "Archivierte neue Fassung" in effective["markdown"]
    assert "Fassung 2026" not in before["markdown"]
    assert before["partial"] is True
    assert effective["partial"] is True
    assert any(gap["reason"] == "missing_old_new"
               for gap in before["gaps"])


def test_empty_side_is_not_treated_as_a_whole_norm_lifecycle() -> None:
    act = _act(versions=[{
        "date": "2025-01-01",
        "text": "Absatz eingefügt",
        "changes": [{"para": "24", "old": "", "new": "Neuer Text"}],
    }])
    snapshot = render_markdown_snapshot(
        act, requested_at="2024-12-31", norm="24",
        fallback_head="2026-07-15")
    # The known current text stays visible, explicitly marked partial; the API
    # does not delete § 24 based on a possibly paragraph-level empty side.
    assert "Neuer Text" in snapshot["markdown"]
    assert snapshot["partial"] is True
    assert any(gap["reason"] == "empty_change_side"
               for gap in snapshot["gaps"])


def test_truncated_and_metadata_only_transitions_are_explicit_gaps() -> None:
    act = _act(versions=[
        {"date": "2025-03-01", "text": "Metadata only"},
        {"date": "2025-02-01", "text": "Captured synopse", "changes": [{
            "para": "24", "old": "A" * 1200, "new": "B" * 1200,
        }]},
    ])
    archive = build_archive_index(act, fallback_head="2026-07-15")
    reasons = {gap["reason"] for gap in archive["gaps"]}
    assert {"metadata_only_versions", "truncated_synopse"} <= reasons
    snapshot = render_markdown_snapshot(
        act, requested_at="2025-01-01", fallback_head="2026-07-15")
    reasons = {gap["reason"] for gap in snapshot["gaps"]}
    assert {"missing_old_new", "truncated_synopse"} <= reasons


def test_historical_bare_gg_designator_uses_predominant_article_kind() -> None:
    gg = {
        "id": "fed_gg",
        "jurabk": "GG",
        "juris": "DE",
        "title": "Grundgesetz",
        "norms": [
            {"enbez": "Art. 1", "titel": "Menschenwürde", "text": "Eins"},
            {"enbez": "Art. 2", "titel": "Freiheit", "text": "Zwei"},
        ],
        "versions": [{
            "date": "2008-08-01", "text": "Art. 75 aufgehoben",
            "changes": [{"para": "75", "old": "Historischer Text", "new": ""}],
        }],
    }
    archive = build_archive_index(gg, fallback_head="2026-07-15")
    labels = {row["enbez"] for row in archive["norms"]}
    assert "Art. 75" in labels
    assert "§ 75" not in labels

    snapshot = render_markdown_snapshot(
        gg, requested_at="2008-07-31", norm="Art.75",
        fallback_head="2026-07-15")
    assert snapshot["norm"] == "Art. 75"
    assert snapshot["partial"] is True
    assert "historical designator" in snapshot["markdown"]


def test_ordinary_federal_bare_historical_designator_becomes_section() -> None:
    act = _act(versions=[{
        "date": "2010-01-01", "text": "§ 99 aufgehoben",
        "changes": [{"para": "99", "old": "Alt", "new": ""}],
    }])
    archive = build_archive_index(act, fallback_head="2026-07-15")
    assert "§ 99" in {row["enbez"] for row in archive["norms"]}


def test_invalid_future_date_and_unknown_norm_are_rejected() -> None:
    act = _act()
    with pytest.raises(InvalidArchiveDateError):
        render_markdown_snapshot(
            act, requested_at="2026-07-16", fallback_head="2026-07-15")
    with pytest.raises(InvalidArchiveDateError):
        render_markdown_snapshot(
            act, requested_at="not-a-date", fallback_head="2026-07-15")
    with pytest.raises(UnknownNormError):
        render_markdown_snapshot(
            act, norm="§ 404", fallback_head="2026-07-15")


def test_complete_official_observation_is_exact_but_not_effective_date() -> None:
    digest = "a" * 64
    act = _act(norms=[
        {"enbez": "§ 24", "titel": "Schutz", "text": "HEAD"},
    ])
    act["official_states"] = [{
        "observed_at": "2026-07-13",
        "state_sha256": digest,
        "norm_count": 1,
        "builddate": "20260712010101",
        "source_url": "https://www.gesetze-im-internet.de/testg/",
        "date_basis": "retrieval_observation_not_effective_date",
        "verification": "exact",
    }]
    state = {
        **act["official_states"][0],
        "source": "GII",
        "norms": [{"enbez": "§ 24", "titel": "Schutz",
                   "text": "Beobachteter amtlicher Text"}],
    }

    archive = build_archive_index(act, fallback_head="2026-07-15")
    observed = next(row for row in archive["entries"]
                    if row["date"] == "2026-07-13")
    assert observed["exact"] is True
    assert observed["partial"] is False
    assert observed["date_basis"] == \
        "retrieval_observation_not_effective_date"
    assert observed["state_digest"] == digest

    rendered = render_markdown_snapshot(
        act, requested_at="2026-07-13", norm="§ 24",
        fallback_head="2026-07-15", observed_state=state)
    assert rendered["exact"] is True
    assert rendered["partial"] is False
    assert rendered["state_sha256"] == digest
    assert "Beobachteter amtlicher Text" in rendered["markdown"]
    assert "not an inferred legal effective date" in rendered["markdown"]
    assert "date_basis: \"retrieval_observation_not_effective_date\"" in \
        rendered["markdown"]


def test_reviewed_legal_transition_exposes_provenance_without_claiming_exact() -> None:
    act = _act(norms=[
        {"enbez": "§ 29a", "titel": "Bericht", "text": "Neue Fassung"},
    ], versions=[{
        "date": "2026-07-10",
        "published_at": "2026-07-09",
        "effective_at": "2026-07-10",
        "observed_at": "2026-07-13",
        "date_basis": "official_bgbl_command_and_commencement_clause",
        "verification": "official_final_text_and_complete_state_pair",
        "legal_effect_verified": True,
        "review_id": "fed-review:test",
        "procedure_id": "327966",
        "source_url": "https://www.recht.bund.de/example.pdf",
        "text": "Amtliche BGBl-Änderung · Inkrafttreten geprüft",
        "changes": [{
            "para": "§ 29a", "effective_date": "2026-07-10",
            "old": "Alte Fassung", "new": "Neue Fassung",
            "source": "official_bgbl_review",
        }],
    }])

    archive = build_archive_index(act, fallback_head="2026-07-15")
    entry = next(row for row in archive["entries"]
                 if row["date"] == "2026-07-10")
    assert entry["legal_effect_verified"] is True
    assert entry["published_at"] == "2026-07-09"
    assert entry["observed_at"] == "2026-07-13"
    assert entry["exact"] is False

    rendered = render_markdown_snapshot(
        act, requested_at="2026-07-10", norm="§ 29a",
        fallback_head="2026-07-15")
    assert rendered["legal_effect_verified"] is True
    assert rendered["effective_at"] == "2026-07-10"
    assert rendered["published_at"] == "2026-07-09"
    assert rendered["partial"] is True
    assert "legal_effect_verified: true" in rendered["markdown"]
    assert "final BGBl command" in rendered["markdown"]
