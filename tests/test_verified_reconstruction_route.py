from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api import main
from api.server import INDEX_HTML, SERVICE_INDEX


def _payload() -> dict:
    return {
        "schema_version": 1,
        "kind": "lexgraph-reviewed-verified-reconstructions",
        "built_at": "2026-07-16T12:00:00Z",
        "source_policy": {"official_only": True},
        "reconstructions": [{
            "id": "fed_sgb_8:2026-04-29:2026-06-12",
            "act_id": "fed_sgb_8",
            "jurabk": "SGB 8",
            "effective_from": "2026-04-29",
            "effective_to": "2026-06-12",
            "text_status": "derived_verified",
            "body_complete": True,
            "source_exact": False,
            "reverse_replay_verified": True,
            "state_sha256": "a" * 64,
            "anchor_state_sha256": "b" * 64,
        }],
    }


def test_verified_reconstructions_route_filters_without_crossing_boundary(
        tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "verified_reconstructions.json").write_text(
        json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_CACHE", {})

    with TestClient(main.app) as client:
        response = client.get(
            "/verified-reconstructions", params={"act": "sgb viii"})
        empty_response = client.get(
            "/verified-reconstructions", params={"act": "AufenthG"})

    assert response.status_code == 200
    result = response.json()

    assert result["kind"] == \
        "lexgraph-reviewed-verified-reconstructions"
    assert result["total"] == 1
    row = result["reconstructions"][0]
    assert row["body_complete"] is True
    assert row["reverse_replay_verified"] is True
    assert row["source_exact"] is False
    assert row["text_status"] == "derived_verified"
    assert "state_objects" not in result

    empty = empty_response.json()
    assert empty["total"] == 0
    assert empty["reconstructions"] == []


def test_verified_reconstructions_is_discoverable_from_both_indexes() -> None:
    assert "/verified-reconstructions" in SERVICE_INDEX["endpoints"]
    assert 'href="verified-reconstructions"' in INDEX_HTML
    assert "never source-exact" in INDEX_HTML
