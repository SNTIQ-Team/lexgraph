from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from update_procedure_watch import update_watch_state  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_appends_only_changes_and_stops_after_terminal(tmp_path: Path) -> None:
    watch = tmp_path / "watch.json"
    state = tmp_path / "state.json"
    history = tmp_path / "history.jsonl"
    _write(watch, {"procedures": {"1": {
        "id": "bill", "source": "DIP", "monitor": True}}})
    dip = [{"id": "1", "titel": "Bill", "beratungsstand": "Überwiesen",
            "datum": "2026-01-01", "aktualisiert": "2026-01-02"}]

    first = update_watch_state(watch, state, history, dip, [], "2026-07-15T08:00:00Z")
    assert first["state"]["procedures"]["1"]["active"] is True
    assert len(first["changes"]) == 1

    same = update_watch_state(watch, state, history, dip, [], "2026-07-15T20:00:00Z")
    assert same["changes"] == []
    assert same["state"]["procedures"]["1"]["last_checked"] == "2026-07-15T20:00:00Z"

    dip[0].update({"beratungsstand": "Verkündet",
                   "aktualisiert": "2026-07-16",
                   "verkuendung": [{"fundstelle": "BGBl I 2026, 1"}]})
    terminal = update_watch_state(
        watch, state, history, dip, [], "2026-07-16T08:00:00Z")
    row = terminal["state"]["procedures"]["1"]
    assert row["terminal"] is True
    assert row["active"] is False
    assert row["tracking_state"] == "terminal"

    # A terminal record is an immutable archive and no longer accrues polls.
    dip[0]["aktualisiert"] = "2026-07-17"
    stopped = update_watch_state(
        watch, state, history, dip, [], "2026-07-17T08:00:00Z")
    assert stopped["changes"] == []
    assert stopped["state"]["procedures"]["1"]["last_checked"] == \
        "2026-07-16T08:00:00Z"
    assert len(history.read_text().splitlines()) == 2


def test_eu_requires_fetcher_terminal_evidence(tmp_path: Path) -> None:
    watch = tmp_path / "watch.json"
    state = tmp_path / "state.json"
    history = tmp_path / "history.jsonl"
    _write(watch, {"procedures": {"eu-x": {
        "id": "eu", "source": "EUR-Lex", "monitor": True}}})
    council = {"source": "Council public register", "document": "ST 11375/26",
               "date": "2026-07-10", "stage": "Political agreement",
               "fetched_at": "2026-07-15T20:00:00Z", "terminal": False}
    row = {"id": "eu-x", "procedure": "2026/1/NLE", "status": "Ongoing",
           "stage": "Political agreement", "terminal": False,
           "council_development": council,
           "adopted_celexes": [], "official_journal": []}
    result = update_watch_state(
        watch, state, history, [], [row], "2026-07-15T20:00:00Z")
    current = result["state"]["procedures"]["eu-x"]
    assert current["active"] is True
    assert current["status"] == "Ongoing"
    assert current["council_development"] == council
    assert result["changes"][0]["council_development"] == council

    # A later successful poll with identical official metadata must not
    # manufacture a status-history entry solely because it was fetched later.
    council["fetched_at"] = "2026-07-16T08:00:00Z"
    same = update_watch_state(
        watch, state, history, [], [row], "2026-07-16T08:00:00Z")
    assert same["changes"] == []


def test_eu_oj_without_final_review_remains_active(tmp_path: Path) -> None:
    watch = tmp_path / "watch.json"
    state = tmp_path / "state.json"
    history = tmp_path / "history.jsonl"
    _write(watch, {"procedures": {"eu-x": {
        "id": "eu", "source": "EUR-Lex", "monitor": True}}})
    row = {
        "id": "eu-x", "procedure": "2026/1/NLE", "status": "Completed",
        "stage": "Publication in the Official Journal", "terminal": False,
        "publication_detected": True, "awaiting_final_review": True,
        "adopted_celexes": ["32026D1999"],
        "official_journal": [{"celex": "32026D1999"}],
    }
    result = update_watch_state(
        watch, state, history, [], [row], "2026-07-20T20:00:00Z")
    current = result["state"]["procedures"]["eu-x"]
    assert current["active"] is True
    assert current["terminal"] is False
    assert current["tracking_state"] == "pending_final_review"


def test_missing_source_is_explicit_and_reappearance_is_recorded(
        tmp_path: Path) -> None:
    watch = tmp_path / "watch.json"
    state = tmp_path / "state.json"
    history = tmp_path / "history.jsonl"
    _write(watch, {"procedures": {"1": {
        "id": "bill", "source": "DIP", "monitor": True}}})
    dip = [{"id": "1", "titel": "Bill", "beratungsstand": "Überwiesen",
            "datum": "2026-01-01", "aktualisiert": "2026-01-02"}]
    update_watch_state(watch, state, history, dip, [], "2026-07-15T08:00:00Z")

    missing = update_watch_state(
        watch, state, history, [], [], "2026-07-15T20:00:00Z")
    row = missing["state"]["procedures"]["1"]
    assert row["status"] == "Not found in latest official snapshot"
    assert row["stage"] == "source_missing"
    assert row["tracking_state"] == "source_missing"
    assert row["last_observed_status"] == "Überwiesen"
    assert missing["changes"][0]["event"] == "source_missing"

    restored = update_watch_state(
        watch, state, history, dip, [], "2026-07-16T08:00:00Z")
    row = restored["state"]["procedures"]["1"]
    assert row["status"] == "Überwiesen"
    assert row["tracking_state"] == "active"
    assert restored["changes"][0]["event"] == "source_restored"
    assert len(history.read_text().splitlines()) == 3

    missing_again = update_watch_state(
        watch, state, history, [], [], "2026-07-16T20:00:00Z")
    assert missing_again["changes"][0]["event"] == "source_missing"
    events = [json.loads(line) for line in history.read_text().splitlines()]
    missing_ids = [event["event_id"] for event in events
                   if event["event"] == "source_missing"]
    assert len(missing_ids) == len(set(missing_ids)) == 2


def test_history_append_is_idempotent_if_state_replace_was_lost(
        tmp_path: Path) -> None:
    watch = tmp_path / "watch.json"
    state = tmp_path / "state.json"
    history = tmp_path / "history.jsonl"
    _write(watch, {"procedures": {"1": {
        "id": "bill", "source": "DIP", "monitor": True}}})
    initial = [{"id": "1", "titel": "Bill", "beratungsstand": "Überwiesen",
                "datum": "2026-01-01", "aktualisiert": "2026-01-02"}]
    update_watch_state(watch, state, history, initial, [],
                       "2026-07-15T08:00:00Z")
    old_state = state.read_text(encoding="utf-8")
    changed = [dict(initial[0], beratungsstand="Verabschiedet",
                    aktualisiert="2026-07-16")]
    update_watch_state(watch, state, history, changed, [],
                       "2026-07-16T08:00:00Z")
    lines_after_append = history.read_text(encoding="utf-8").splitlines()
    transition = json.loads(lines_after_append[-1])

    # Simulate a crash after the fsynced history append but before atomic state
    # replacement. The retry must reuse the same transition id.
    state.write_text(old_state, encoding="utf-8")
    retried = update_watch_state(watch, state, history, changed, [],
                                 "2026-07-16T08:05:00Z")
    assert retried["changes"] == []
    assert history.read_text(encoding="utf-8").splitlines() == lines_after_append
    current = retried["state"]["procedures"]["1"]
    assert current["status"] == "Verabschiedet"
    assert current["last_changed"] == transition["observed_at"]
