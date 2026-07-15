from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402


def test_temporal_ignores_unverified_draft_bill_dates() -> None:
    rows = [
        {"status": "published", "valid_from": "2026-01-01"},
        {"status": "adopted", "valid_from": "2025-12-01"},
        {"status": "proposed", "valid_from": "2027-01-01"},
    ]

    result = web_data.temporal([], [], rows)

    assert result["last_change"] is None
    assert result["first_change"] is None
    assert result["change_count"] == 0
    assert result["next_change"] is None
    assert result["pending"] == 2


def test_temporal_accepts_only_explicitly_verified_patch_date() -> None:
    result = web_data.temporal(
        [], [],
        [{"status": "published", "valid_from": "2026-01-01",
          "valid_from_verified": True}],
    )

    assert result["last_change"] == "2026-01-01"
    assert result["change_count"] == 1
