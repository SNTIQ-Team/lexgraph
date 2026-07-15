from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import lex_blame  # noqa: E402
import lex_log  # noqa: E402
from official_states import archive_gii_states  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")


def _snapshot(root: Path, day: str, text: str, builddate: str) -> Path:
    path = root / day
    _write_jsonl(path / "acts.jsonl", [{
        "slug": "demog", "jurabk": "DemoG",
        "long_title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "builddate": builddate, "doknr": "A1", "norm_count": 2,
    }])
    _write_jsonl(path / "norms.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 1",
        "titel": "Zweck", "text": text, "doknr": "N1",
        "gliederung": "Teil 1",
    }, {
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 2",
        "titel": "Andere Norm", "text": "nicht angefordert",
        "doknr": "N2", "gliederung": "Teil 1",
    }])
    return path


def _store(tmp_path: Path) -> tuple[Path, dict]:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    old = _snapshot(snapshots, "2026-07-13", "alter Text",
                    "20260712010101")
    new = _snapshot(snapshots, "2026-07-15", "neuer Text",
                    "20260714010101")
    return store, archive_gii_states([old, new], store)


def _act() -> dict:
    return {"jurabk": "DemoG", "long_title": "Demonstrationsgesetz"}


def test_federal_checkout_emits_only_exact_observed_norm_as_markdown(
        tmp_path: Path, monkeypatch, capsys) -> None:
    store, manifest = _store(tmp_path)
    monkeypatch.setattr(lex_blame, "OFFICIAL_STORE", store)
    monkeypatch.setattr(lex_blame, "resolve_act",
                        lambda _query: (_act(), "federal"))

    result = lex_blame.cmd_checkout(argparse.Namespace(
        act="DemoG", at="2026-07-13", norm="1"))
    output = capsys.readouterr().out

    assert result == 0
    assert output.startswith("---\n")
    assert 'scope: "§ 1"' in output
    assert "alter Text" in output
    assert "nicht angefordert" not in output
    observation = next(row for row in manifest["observations"]
                       if row["observed_at"] == "2026-07-13")
    assert observation["state_sha256"] in output
    assert 'date_basis: "retrieval_observation_not_effective_date"' \
        in output
    assert "not an inferred legal effective date" in output


def test_federal_checkout_refuses_nearest_observation(
        tmp_path: Path, monkeypatch, capsys) -> None:
    store, _manifest = _store(tmp_path)
    monkeypatch.setattr(lex_blame, "OFFICIAL_STORE", store)
    monkeypatch.setattr(lex_blame, "resolve_act",
                        lambda _query: (_act(), "federal"))

    result = lex_blame.cmd_checkout(argparse.Namespace(
        act="DemoG", at="2026-07-14", norm=None))
    output = capsys.readouterr().out

    assert result == 2
    assert "no exact official GII observation" in output
    assert "2026-07-13, 2026-07-15" in output
    assert "will not silently substitute the nearest state" in output


def test_log_keeps_retrieval_and_verified_legal_dates_in_separate_lanes(
        monkeypatch, capsys) -> None:
    transition = {
        "observed_at": "2026-07-15",
        "previous_observed_at": "2026-07-13",
        "previous_state_sha256": "a" * 64,
        "state_sha256": "b" * 64,
        "source_url": "https://www.gesetze-im-internet.de/demog/",
        "changes": [{"para": "§ 1", "operation": "replace"}],
    }
    review = transition | {
        "effective_at": "2026-07-14", "published_at": "2026-07-13",
        "procedure_id": "123", "amending_articles": ["4"],
        "bgbl": {
            "document_id": "bgbl-1-2026-1",
            "pdf_url": "https://www.recht.bund.de/final.pdf",
        },
    }
    observation = {
        "observed_at": "2026-07-15", "builddate": "20260714010101",
        "norm_count": 2, "state_sha256": "b" * 64,
        "date_basis": "retrieval_observation_not_effective_date",
        "source_url": "https://www.gesetze-im-internet.de/demog/",
    }
    monkeypatch.setattr(lex_log, "official_history", lambda _jurabk: {
        "observations": [observation], "transitions": [transition],
        "reviews": [review],
    })

    lex_log.show_official_federal_history("DemoG", False)
    output = capsys.readouterr().out

    assert "verified legal transitions" in output
    assert "● effective 2026-07-14" in output
    assert "observed GII content transitions" in output
    assert "◆ observed 2026-07-15" in output
    assert "retrieval dates, NOT legal effect" in output
    assert "exact official GII states" in output


def test_blame_emits_observation_and_legal_review_as_distinct_events(
        monkeypatch) -> None:
    transition = {
        "observed_at": "2026-07-15",
        "previous_observed_at": "2026-07-13",
        "previous_state_sha256": "a" * 64,
        "state_sha256": "b" * 64,
        "source_url": "https://www.gesetze-im-internet.de/demog/",
        "changes": [{"para": "§ 1", "operation": "replace"}],
    }
    review = transition | {
        "effective_at": "2026-07-14", "published_at": "2026-07-13",
        "procedure_id": "123", "amending_articles": ["4"],
        "bgbl": {
            "document_id": "bgbl-1-2026-1",
            "pdf_url": "https://www.recht.bund.de/final.pdf",
        },
    }
    monkeypatch.setattr(lex_blame, "official_history", lambda _jurabk: {
        "observations": [], "transitions": [transition],
        "reviews": [review],
    })

    events = lex_blame.official_norm_events("DemoG", "1")

    assert [event["kind"] for event in events] == ["observed", "amended"]
    assert "NOT effective date" in events[0]["badge"]
    assert "BGBl final text + DIP commencement" in events[1]["badge"]
    assert events[0]["label"] == "2026-07-15"
    assert events[1]["label"] == "2026-07-14"
