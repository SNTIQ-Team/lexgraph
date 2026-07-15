from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_watched_payload_merges_persistent_state_config_and_history(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    state = tmp_path / "state.json"
    history = tmp_path / "history.jsonl"
    _write(watchlist, {"procedures": {
        "eu-x": {
            "id": "temporary-protection", "source": "EUR-Lex",
            "jurisdiction": "EU", "queries": ["Ukraine"],
            "scope": "Draft only", "draft_only": True,
            "council_register_document": "ST 11375/26",
            "council_register_url": "https://example.test/register",
        },
        "de-x": {
            "id": "finished", "source": "DIP", "monitor": False,
            "validation_ids": ["validation-x"],
        },
    }})
    _write(state, {"schema_version": 1, "checked_at": "2026-07-15T08:00:00Z",
                   "procedures": {
        "eu-x": {"id": "eu-x", "source": "EUR-Lex", "title": "EU draft",
                 "status": "Ongoing", "active": True, "terminal": False,
                 "council_development": {"document": "ST 11375/26",
                                         "stage": "Political agreement",
                                         "terminal": False},
                 "tracking_state": "active", "last_checked": "2026-07-15T08:00:00Z"},
        "de-x": {"id": "de-x", "source": "DIP", "title": "Final act",
                 "status": "Verkündet", "active": False, "terminal": True,
                 "tracking_state": "terminal", "last_checked": "2026-07-14T08:00:00Z"},
    }})
    history.write_text(json.dumps({
        "id": "eu-x", "event": "first_seen",
        "observed_at": "2026-07-15T08:00:00Z",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(web_data, "PROCEDURE_WATCHLIST", watchlist)
    monkeypatch.setattr(web_data, "PROCEDURE_WATCH_STATE", state)
    monkeypatch.setattr(web_data, "PROCEDURE_WATCH_HISTORY", history)

    payload = web_data.build_watched_procedures({})

    assert payload["active_count"] == 1
    assert payload["terminal_count"] == 1
    assert payload["checked_at"] == "2026-07-15T08:00:00Z"
    active = payload["procedures"][0]
    assert active["id"] == "eu-x"
    assert active["queries"] == ["Ukraine"]
    assert active["scope"] == "Draft only"
    assert active["draft_only"] is True
    assert active["council_register_document"] == "ST 11375/26"
    assert active["council_register_url"] == "https://example.test/register"
    assert active["council_development"]["stage"] == "Political agreement"
    assert active["history"][0]["event"] == "first_seen"
    final = payload["procedures"][1]
    assert final["validation_ids"] == ["validation-x"]


def test_amendment_fate_validates_only_declared_current_law_checks(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "fates.json"
    _write(source, {"schema_version": 1, "records": [{
        "id": "x", "procedure_id": "1", "document_chain": [{"role": "draft"}],
        "current_law_checks": [
            {"type": "norm_absent", "act_id": "fed_stag", "norm": "§ 43"},
            {"type": "norm_text_contains", "act_id": "fed_vwgo",
             "norm": "§ 75", "text": "nicht vor Ablauf von drei Monaten"},
        ],
    }]})
    monkeypatch.setattr(web_data, "AMENDMENT_FATES", source)
    details = {
        "fed_stag": {"norms": [{"enbez": "§ 42", "text": "other"}]},
        "fed_vwgo": {"norms": [{
            "enbez": "§ 75",
            "text": "Die Klage kann nicht vor Ablauf von drei Monaten erhoben werden.",
        }]},
    }

    payload = web_data.build_amendment_fates(
        details, checked_at="2026-07-15T09:00:00Z")

    assert payload["total"] == 1
    assert payload["validated"] == 1
    validation = payload["records"][0]["validation"]
    assert validation["passed"] is True
    assert [check["reason"] for check in validation["checks"]] == [
        "norm_absent", "text_found"]
