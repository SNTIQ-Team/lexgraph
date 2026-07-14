from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines()]


def _snapshot(root: Path, day: str, rows: list[dict]) -> None:
    _write_jsonl(root / "bayern_recht" / day / "norms.jsonl", rows)


def _redirect_storage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    snapshots = tmp_path / "snapshots"
    ledger = tmp_path / "data" / "by_diffs.jsonl"
    monkeypatch.setattr(web_data, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(web_data, "BY_DIFF_LEDGER", ledger)
    return snapshots, ledger


def test_whole_act_curation_changes_do_not_create_fake_diffs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots, ledger = _redirect_storage(tmp_path, monkeypatch)
    existing = {
        "jurabk": "AlreadyTracked",
        "date": "2026-07-12",
        "para": "Art. 9",
        "old": "vorher",
        "new": "nachher",
        "source": "daily_snapshot",
    }
    _write_jsonl(ledger, [existing])
    _snapshot(snapshots, "2026-07-13", [
        {"jurabk": "RemovedAct", "enbez": "Art. 1", "text": "old"},
        {"jurabk": "StableAct", "enbez": "Art. 1", "text": "same"},
    ])
    _snapshot(snapshots, "2026-07-14", [
        {"jurabk": "NewAct", "enbez": "Art. 1", "text": "new"},
        {"jurabk": "StableAct", "enbez": "Art. 1", "text": "same"},
    ])

    web_data._update_by_diff_ledger()

    assert _read_jsonl(ledger) == [existing]


def test_shared_act_norm_transitions_append_complete_daily_diffs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots, ledger = _redirect_storage(tmp_path, monkeypatch)
    existing = {
        "jurabk": "AlreadyTracked",
        "date": "2026-07-12",
        "para": "Art. 9",
        "old": "vorher",
        "new": "nachher",
        "source": "wayback",
    }
    _write_jsonl(ledger, [existing])

    old_text = "Alter vollständiger Text: " + ("A" * 20_000) + " :Ende"
    new_text = "Neuer vollständiger Text: " + ("B" * 21_000) + " :Ende"
    repealed_text = "Aufgehobener vollständiger Text " + ("R" * 12_000)
    introduced_text = "Eingefügter vollständiger Text " + ("N" * 13_000)
    _snapshot(snapshots, "2026-07-13", [
        {"jurabk": "SharedAct", "enbez": "Art. 1", "text": old_text},
        {"jurabk": "SharedAct", "enbez": "Art. 2", "text": repealed_text},
    ])
    _snapshot(snapshots, "2026-07-14", [
        {"jurabk": "SharedAct", "enbez": "Art. 1", "text": new_text},
        {"jurabk": "SharedAct", "enbez": "Art. 3", "text": introduced_text},
    ])

    web_data._update_by_diff_ledger()

    rows = _read_jsonl(ledger)
    assert rows[0] == existing
    appended = {row["para"]: row for row in rows[1:]}
    assert set(appended) == {"Art. 1", "Art. 2", "Art. 3"}
    assert all(row["jurabk"] == "SharedAct" for row in appended.values())
    assert all(row["date"] == "2026-07-14" for row in appended.values())
    assert all(row["source"] == "daily_snapshot"
               for row in appended.values())
    assert appended["Art. 1"]["old"] == old_text
    assert appended["Art. 1"]["new"] == new_text
    assert appended["Art. 2"]["old"] == repealed_text
    assert appended["Art. 2"]["new"] == ""
    assert appended["Art. 3"]["old"] == ""
    assert appended["Art. 3"]["new"] == introduced_text
