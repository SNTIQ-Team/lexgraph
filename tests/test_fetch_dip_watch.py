from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_dip import fetch_watched_positions  # noqa: E402


class Response:
    def __init__(self, payload=None, text="", status=200):
        self._payload = payload or {}
        self.text = text
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    def get(self, url: str, **kwargs):
        if url.endswith("/vorgangsposition"):
            return Response({"documents": [{
                "id": "p1", "vorgang_id": "1", "datum": "2026-01-12",
                "vorgangsposition": "Gesetzentwurf", "zuordnung": "BT",
                "fundstelle": {"id": "doc1", "dokumentnummer": "21/3539",
                               "pdf_url": "https://official.test/draft"},
            }]})
        if "/drucksache-text/" in url:
            return Response({"text": "erstmals nach dem 31. März 2025 erteilt"})
        if url == "https://official.test/hearing":
            return Response(text="Anhörung 23. Februar 2026 21/3539")
        raise AssertionError(url)


def test_fetches_position_chain_and_rechecks_official_content() -> None:
    rows = fetch_watched_positions(FakeHttp(), "key", {"1": {
        "content_checks": [{
            "id": "cutoff", "document_number": "21/3539",
            "required_patterns": [r"31\. März 2025 erteilt"],
            "source_url": "https://official.test/draft",
        }],
        "official_evidence_checks": [{
            "id": "hearing", "date": "2026-02-23",
            "label": "Öffentliche Anhörung",
            "url": "https://official.test/hearing",
            "required_patterns": ["Anhörung", "21/3539"],
        }],
    }})

    assert len(rows) == 2
    assert rows[0]["content_validations"][0]["passed"] is True
    assert rows[1]["vorgangsposition"] == "Öffentliche Anhörung"
    assert rows[1]["content_validations"][0]["passed"] is True

