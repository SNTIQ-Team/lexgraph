from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from official_states import canonical_json_bytes  # noqa: E402
from verified_reconstruction import (  # noqa: E402
    ARTIFACT_KIND,
    REVIEW_KIND,
    ReconstructionError,
    build_reconstructions,
)


INCOMING_TEXT = (
    "(2d) Abweichend von Absatz 1 dürfen das Ergebnis der Altersfeststellung "
    "aus dem Verfahren nach § 42f sowie, soweit der Vertreter der betroffenen "
    "Person einwilligt, die auf Grundlage von § 42f Absatz 1 Satz 1 erlangten "
    "Erkenntnisse dem Bundesamt für Migration und Flüchtlinge auf Ersuchen "
    "für die Erfüllung von Aufgaben nach § 5 Absatz 1 des Asylgesetzes "
    "übermittelt werden."
)
INSERTED_SENTENCE = "§ 55 Absatz 3 und 5 gilt entsprechend."
ANCHOR_STAND = (
    "Neuf: Neugefasst durch Bek. v. 11.9.2012 I 2022; | Stand: zuletzt "
    "geändert durch Art. 9 G v. 23.4.2026 I Nr. 111"
)
PDF_SHA = "6" * 64
TEXT_SHA = "a" * 64


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _candidate(
        *, candidate_id: str, article: str, effective_at: str,
        status: str, article_sha: str, commands: list[dict],
        affected: list[str]) -> dict:
    return {
        "id": candidate_id,
        "act_id": "fed_sgb_8",
        "jurabk": "SGB 8",
        "document_id": "bgbl-1-2026-111",
        "procedure_id": "325560",
        "amending_article": article,
        "execution_date": "2026-04-23",
        "publication_date": "2026-04-28",
        "effective_at": effective_at,
        "effective_date_status": status,
        "pdf_sha256": PDF_SHA,
        "pdf_md5": "d" * 32,
        "advertised_md5": "d" * 32,
        "text_sha256": TEXT_SHA,
        "article_text_sha256": article_sha,
        "official_html_url": (
            "https://www.recht.bund.de/bgbl/1/2026/111/VO.html"),
        "official_pdf_url": (
            "https://www.recht.bund.de/bgbl/1/2026/111/regelungstext.pdf"),
        "candidate_only": True,
        "historical_text_reconstructed": False,
        "integrity_verified": True,
        "command_scope_status": "whole_article",
        "collective_subsection": None,
        "commands": commands,
        "command_count": len(commands),
        "affected_norms": affected,
    }


def _fixture() -> tuple[dict, list[dict], dict, dict[str, dict]]:
    anchor_state = {
        "id": "fed_sgb_8",
        "jurabk": "SGB 8",
        "juris": "DE",
        "title": "Sozialgesetzbuch VIII",
        "stand": ANCHOR_STAND,
        "build": "20260611",
        "norm_count": 3,
        "norms": [{
            "enbez": "§ 42",
            "titel": "Inobhutnahme",
            "text": (
                "(1) Vorher.(2) Eins. Zwei. Drei. Vier. Fünfte Einreichung."),
            "glied": "Dritter Abschnitt",
        }, {
            "enbez": "§ 42a",
            "titel": "Vorläufige Inobhutnahme",
            "text": (
                f"(3) Eins. Zwei. {INSERTED_SENTENCE}(3a) Folge."),
            "glied": "Dritter Abschnitt",
        }, {
            "enbez": "§ 64",
            "titel": "Datenübermittlung",
            "text": f"(2c) Vorher.{INCOMING_TEXT}(3) Danach.",
            "glied": "Viertes Kapitel",
        }],
    }
    anchor_sha = _sha(anchor_state)
    incoming_commands = [{
        "operation": "insert",
        "ref": {"para": "64", "absatz": "2c", "satz": "1"},
        "old_text_constraint": None,
        "new_text": INCOMING_TEXT,
        "raw": (
            "In § 64 wird nach Absatz 2c der folgende Absatz 2d eingefügt: "
            f"„{INCOMING_TEXT}“"),
    }]
    outgoing_commands = [{
        "operation": "replace",
        "ref": {"para": "42", "absatz": "2", "satz": "5"},
        "old_text_constraint": "Stellung",
        "new_text": "Einreichung",
        "raw": (
            "In § 42 Absatz 2 Satz 5 wird die Angabe „Stellung“ durch die "
            "Angabe „Einreichung“ ersetzt."),
    }, {
        "operation": "insert",
        "ref": {"para": "42a", "absatz": "3", "satz": "2"},
        "old_text_constraint": None,
        "new_text": INSERTED_SENTENCE,
        "raw": (
            "In § 42a Absatz 3 wird nach Satz 2 der folgende Satz eingefügt: "
            f"„{INSERTED_SENTENCE}“"),
    }]
    incoming = _candidate(
        candidate_id="incoming", article="8", effective_at="2026-04-29",
        status="resolved_default_clause", article_sha="1" * 64,
        commands=incoming_commands, affected=["§ 64"])
    outgoing = _candidate(
        candidate_id="outgoing", article="9", effective_at="2026-06-12",
        status="resolved_explicit_article_clause", article_sha="2" * 64,
        commands=outgoing_commands, affected=["§ 42", "§ 42a"])

    def event(candidate: dict, expected_commands: list[dict]) -> dict:
        return {
            "candidate_id": candidate["id"],
            "document_id": candidate["document_id"],
            "procedure_id": candidate["procedure_id"],
            "amending_article": candidate["amending_article"],
            "execution_date": candidate["execution_date"],
            "publication_date": candidate["publication_date"],
            "effective_at": candidate["effective_at"],
            "effective_date_status": candidate["effective_date_status"],
            "pdf_sha256": candidate["pdf_sha256"],
            "pdf_md5": candidate["pdf_md5"],
            "text_sha256": candidate["text_sha256"],
            "article_text_sha256": candidate["article_text_sha256"],
            "expected_commands": expected_commands,
        }

    reviews = {
        "schema_version": 1,
        "kind": REVIEW_KIND,
        "reviews": [{
            "id": "reviewed-sgb8",
            "act_id": "fed_sgb_8",
            "jurabk": "SGB 8",
            "anchor": {
                "state_sha256": anchor_sha,
                "observed_at": "2026-07-15",
                "builddate": "20260611215511",
                "source_url": "https://www.gesetze-im-internet.de/sgb_8/",
                "state_build": "20260611",
                "state_stand": ANCHOR_STAND,
            },
            "interval": {
                "effective_from": "2026-04-29",
                "effective_to": "2026-06-12",
            },
            "incoming": event(incoming, [{
                "kind": "insert_paragraph_after",
                "norm": "§ 64",
                "after_absatz": "2c",
                "new_absatz": "2d",
                "text": INCOMING_TEXT,
            }]),
            "outgoing": event(outgoing, [{
                "kind": "replace_literal",
                "norm": "§ 42",
                "absatz": "2",
                "satz": "5",
                "old": "Stellung",
                "new": "Einreichung",
            }, {
                "kind": "insert_sentence_after",
                "norm": "§ 42a",
                "absatz": "3",
                "after_sentence": "2",
                "text": INSERTED_SENTENCE,
            }]),
        }],
    }
    manifest = {"observations": [{
        "act_id": "fed_sgb_8",
        "jurabk": "SGB 8",
        "state_sha256": anchor_sha,
        "observed_at": "2026-07-15",
        "builddate": "20260611215511",
        "source_url": "https://www.gesetze-im-internet.de/sgb_8/",
        "date_basis": "retrieval_observation_not_effective_date",
        "verification": "exact",
    }]}
    return reviews, [incoming, outgoing], manifest, {anchor_sha: anchor_state}


def _repin_anchor(reviews: dict, manifest: dict, states: dict[str, dict],
                  state: dict) -> None:
    old = next(iter(states))
    digest = _sha(state)
    states.pop(old)
    states[digest] = state
    reviews["reviews"][0]["anchor"]["state_sha256"] = digest
    manifest["observations"][0]["state_sha256"] = digest


def test_verified_sgb8_reconstruction_is_complete_but_not_source_exact() -> None:
    reviews, candidates, manifest, states = _fixture()
    artifact = build_reconstructions(
        reviews, candidates, manifest, states,
        built_at="2026-07-16T12:00:00+00:00")

    assert artifact["kind"] == ARTIFACT_KIND
    row = artifact["reconstructions"][0]
    assert row["effective_from"] == "2026-04-29"
    assert row["effective_to"] == "2026-06-12"
    assert row["knowledge_from"] == "2026-07-16T12:00:00Z"
    assert row["text_status"] == "derived_verified"
    assert row["body_complete"] is True
    assert row["source_exact"] is False
    assert row["reverse_replay_verified"] is True
    assert row["anchor_projection_metadata_retained"] is True
    derived = artifact["state_objects"][row["state_sha256"]]
    by_norm = {norm["enbez"]: norm["text"] for norm in derived["norms"]}
    assert "Stellung" in by_norm["§ 42"]
    assert "Einreichung" not in by_norm["§ 42"]
    assert INSERTED_SENTENCE not in by_norm["§ 42a"]
    assert INCOMING_TEXT in by_norm["§ 64"]
    assert artifact["object_metadata"][row["state_sha256"]]["origin"] == \
        "derived_verified_reverse_replay"


def test_reconstruction_fails_on_ambiguous_command_cardinality() -> None:
    reviews, candidates, manifest, states = _fixture()
    state = copy.deepcopy(next(iter(states.values())))
    norm = next(row for row in state["norms"] if row["enbez"] == "§ 42a")
    norm["text"] = norm["text"].replace(
        INSERTED_SENTENCE, INSERTED_SENTENCE * 2)
    _repin_anchor(reviews, manifest, states, state)

    with pytest.raises(ReconstructionError, match="cardinality"):
        build_reconstructions(
            reviews, candidates, manifest, states,
            built_at="2026-07-16T12:00:00Z")


@pytest.mark.parametrize("mutation, message", [
    ("truncated", "truncated or partial"),
    ("wrong_ref", "ref disagrees"),
    ("extra_command", "commands differ"),
])
def test_reconstruction_rejects_partial_mistargeted_or_extra_commands(
        mutation: str, message: str) -> None:
    reviews, candidates, manifest, states = _fixture()
    outgoing = next(row for row in candidates if row["id"] == "outgoing")
    if mutation == "truncated":
        outgoing["commands"][0]["raw"] = "x" * 800
    elif mutation == "wrong_ref":
        outgoing["commands"][0]["ref"]["para"] = "99"
    else:
        outgoing["commands"].append(copy.deepcopy(outgoing["commands"][0]))
        outgoing["command_count"] += 1

    with pytest.raises(ReconstructionError, match=message):
        build_reconstructions(
            reviews, candidates, manifest, states,
            built_at="2026-07-16T12:00:00Z")


def test_reconstruction_requires_incoming_state_boundary() -> None:
    reviews, candidates, manifest, states = _fixture()
    state = copy.deepcopy(next(iter(states.values())))
    norm = next(row for row in state["norms"] if row["enbez"] == "§ 64")
    norm["text"] = norm["text"].replace("übermittelt werden", "gelöscht")
    _repin_anchor(reviews, manifest, states, state)

    with pytest.raises(ReconstructionError, match="body differs"):
        build_reconstructions(
            reviews, candidates, manifest, states,
            built_at="2026-07-16T12:00:00Z")


def test_checked_in_sgb8_review_is_strict_and_official_only() -> None:
    review = json.loads((
        ROOT / "data" / "verified_reconstruction_reviews.json"
    ).read_text(encoding="utf-8"))
    assert review["kind"] == REVIEW_KIND
    row = review["reviews"][0]
    assert row["act_id"] == "fed_sgb_8"
    assert row["interval"] == {
        "effective_from": "2026-04-29",
        "effective_to": "2026-06-12",
    }
    assert row["anchor"]["source_url"].startswith(
        "https://www.gesetze-im-internet.de/")
    assert row["incoming"]["pdf_sha256"] == row["outgoing"]["pdf_sha256"]
