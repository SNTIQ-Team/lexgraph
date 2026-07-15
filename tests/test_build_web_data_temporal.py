from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402


def test_temporal_uses_only_published_official_patch_as_past_change() -> None:
    rows = [
        {"status": "published", "valid_from": "2026-01-01"},
        {"status": "adopted", "valid_from": "2025-12-01"},
        {"status": "proposed", "valid_from": "2027-01-01"},
    ]

    result = web_data.temporal([], [], rows)

    assert result["last_change"] == "2026-01-01"
    assert result["first_change"] == "2026-01-01"
    assert result["change_count"] == 1
    assert result["next_change"] == "2027-01-01"
    assert result["pending"] == 2


def test_temporal_deduplicates_version_and_published_patch_dates() -> None:
    result = web_data.temporal(
        [{"date": "2026-01-01"}], [],
        [{"status": "published", "valid_from": "2026-01-01"}],
    )

    assert result["change_count"] == 1
