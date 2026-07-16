from __future__ import annotations

import gzip
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api import main  # noqa: E402


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def _state(data: Path, text: str, *, second: bool = False) -> str:
    norms = [{"enbez": "§ 1", "titel": "Zweck", "text": text}]
    if second:
        norms.append({"enbez": "§ 2", "titel": "Neu", "text": "Zwei"})
    value = {
        "schema_version": 1,
        "act_id": "fed_testg",
        "jurabk": "TestG",
        "title": "Testgesetz",
        "norms": norms,
    }
    payload = _canonical(value)
    digest = hashlib.sha256(payload).hexdigest()
    path = (data / "federal_states" / "objects" / "sha256"
            / digest[:2] / f"{digest}.json.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb", mtime=0) as handle:
        handle.write(payload)
    return digest


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _data_plane(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    old = _state(data, "Alt")
    new = _state(data, "Neu", second=True)
    _write(data / "summary.json", {
        "built_at": "2026-07-16T12:00:00Z",
    })
    _write(data / "acts" / "fed_testg.json", {
        "id": "fed_testg", "jurabk": "TestG", "juris": "DE",
        "title": "Testgesetz", "versions": [],
        "norms": [{"enbez": "§ 1", "titel": "Zweck", "text": "Neu"},
                  {"enbez": "§ 2", "titel": "Neu", "text": "Zwei"}],
    })
    objects = {
        old: {"act_id": "fed_testg", "jurabk": "TestG", "norm_count": 1},
        new: {"act_id": "fed_testg", "jurabk": "TestG", "norm_count": 2},
    }
    common = {
        "knowledge_from": "2026-07-16T10:00:00Z",
        "knowledge_to": None,
        "text_status": "official_exact",
        "date_status": "official_verified",
        "date_basis": "official_bgbl_commencement_clause",
        "verification": "test",
        "gaps": [],
        "evidence": [{"source": "BGBl", "url": "https://example.test/bgbl"}],
    }
    _write(data / "retrospective_history.json", {
        "schema_version": 1,
        "kind": "lexgraph-retrospective-history",
        "built_at": "2026-07-16T12:00:00Z",
        "state_identity": "sha256-canonical-uncompressed-json",
        "date_semantics": {
            "effective": "[effective_from,effective_to)",
            "knowledge": "[knowledge_from,knowledge_to)",
        },
        "source_policy": {"official_only": True},
        "objects": objects,
        "acts": {"fed_testg": {
            "act_id": "fed_testg", "jurabk": "TestG",
            "title": "Testgesetz", "history_start": "2024-01-01",
            "events": [{
                "id": "event", "act_id": "fed_testg",
                "date": "2025-01-01",
                "published_at": "2024-12-20",
                "effective_at": "2025-01-01",
                "observed_at": "2026-07-16",
                "ingested_at": "2026-07-16T09:00:00Z",
                "knowledge_from": "2026-07-16T10:00:00Z",
                "knowledge_to": None,
                "text_status": "event_only",
                "date_status": "official_verified",
                "date_basis": "official_dip_article_commencement_clause",
                "candidate_only": True,
                "historical_text_reconstructed": False,
                "document_id": "bgbl-1-2024-999",
                "amending_article": "3",
                "article_heading": "Änderung des Testgesetzes",
                "procedure_id": "12345",
                "procedure_title": "Teständerungsgesetz",
                "official_html_url": "https://example.test/bgbl/html",
                "official_pdf_url": "https://example.test/bgbl/pdf",
                "affected_norms": ["§ 1"],
                "commands": [{
                    "item": 1, "operation": "replace",
                    "ref": {"para": "1"},
                    "old_text_constraint": "Alt", "new_text": "Neu",
                    "raw": "§ 1 wird neu gefasst.",
                }],
                "gaps": [],
                "evidence": [{"source": "BGBl",
                              "url": "https://example.test/bgbl"}],
            }],
            "observations": [{
                "act_id": "fed_testg",
                "observed_at": "2025-01-02", "state_sha256": old,
                "source_url": "https://www.gesetze-im-internet.de/testg/",
            }, {
                "act_id": "fed_testg",
                "observed_at": "2026-07-16", "state_sha256": new,
                "source_url": "https://www.gesetze-im-internet.de/testg/",
            }],
            "gaps": [], "coverage": {"observed_from": "2025-01-02"},
            "intervals": [{
                **common, "id": "old", "effective_from": "2024-01-01",
                "effective_to": "2025-01-01", "published_at": "2023-12-20",
                "observed_at": "2025-01-02",
                "verified_through_observed_at": "2025-01-02",
                "state_sha256": old, "previous_state_sha256": None,
            }, {
                **common, "id": "new", "effective_from": "2025-01-01",
                "effective_to": None, "published_at": "2024-12-20",
                "observed_at": "2026-07-16",
                "verified_through_observed_at": "2026-07-16",
                "state_sha256": new, "previous_state_sha256": old,
            }],
        }},
    })
    (data / "retrospective_history.sqlite").write_bytes(b"SQLite format 3\0")
    return data


def _configure(monkeypatch, data: Path) -> None:
    monkeypatch.setattr(main, "DATA_DIR", data)
    monkeypatch.setattr(main, "_CACHE", {})
    monkeypatch.setattr(main, "_RETROSPECTIVE_MANIFEST", None)
    monkeypatch.setattr(main, "_RETROSPECTIVE_SOURCE", None)


def _json(response) -> dict:
    return json.loads(response.body)


def test_history_archive_diff_and_markdown_use_bitemporal_manifest(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    _configure(monkeypatch, data)
    known = datetime(2026, 7, 16, 11, tzinfo=timezone.utc)

    history = _json(main.retrospective_history("fed_testg", known))
    assert history["kind"] == "lexgraph-retrospective-history"
    assert history["as_of"] == "2026-07-16T11:00:00Z"
    assert len(history["intervals"]) == 2

    archive = _json(main.act_archive("fed_testg", known))
    assert archive["head_date"] == "2026-07-16"
    assert archive["retrospective"]["available"] is True
    assert archive["retrospective"]["history_start"] == "2024-01-01"
    assert archive["retrospective"]["date_semantics"]["knowledge"] == \
        "[knowledge_from,knowledge_to)"

    diff = _json(main.retrospective_diff(
        "fed_testg", date(2024, 6, 1), date(2025, 6, 1), known, None))
    assert [(row["operation"], row["enbez"])
            for row in diff["changes"]] == [
                ("replace", "§ 1"), ("add", "§ 2")]

    markdown = main.act_markdown(
        "fed_testg", date(2024, 6, 1), known, "§ 1", False)
    assert "Alt" in markdown.body.decode("utf-8")
    assert markdown.headers["x-lexgraph-as-of"] == \
        "2026-07-16T11:00:00Z"
    assert markdown.headers["x-lexgraph-effective-from"] == "2024-01-01"
    assert markdown.headers["x-lexgraph-knowledge-from"] == \
        "2026-07-16T10:00:00Z"
    assert markdown.headers["x-lexgraph-state-sha256"]


def test_sqlite_download_has_stable_name(tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    _configure(monkeypatch, data)
    response = main.retrospective_sqlite()
    assert Path(response.path) == data / "retrospective_history.sqlite"
    assert response.media_type == "application/vnd.sqlite3"
    assert response.headers["content-disposition"] == \
        'attachment; filename="lexgraph-retrospective-history.sqlite"'


def test_markdown_route_preserves_verified_reconstruction_truth_flags(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    manifest_path = data / "retrospective_history.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    interval = manifest["acts"]["fed_testg"]["intervals"][0]
    interval.update({
        "text_status": "derived_verified",
        "body_complete": True,
        "source_exact": False,
        "reverse_replay_verified": True,
        "verification": "reviewed_inverse_then_canonical_forward_replay",
    })
    _write(manifest_path, manifest)
    _configure(monkeypatch, data)

    with TestClient(main.app) as client:
        response = client.get(
            "/acts/fed_testg/markdown",
            params={"at": "2024-06-01", "norm": "§ 1"})

    assert response.status_code == 200
    assert response.headers["x-lexgraph-exact"] == "false"
    assert response.headers["x-lexgraph-archive-status"] == \
        "verified_reconstruction"
    assert response.headers["x-lexgraph-complete"] == "true"
    assert response.headers["x-lexgraph-source-exact"] == "false"
    assert response.headers["x-lexgraph-verified-reconstruction"] == "true"
    assert "archive_status: verified_reconstruction" in response.text
    assert "complete: true" in response.text
    assert "source_exact: false" in response.text
    assert "verified_reconstruction: true" in response.text


def test_changes_and_amending_act_preserve_dates_commands_and_evidence(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    _configure(monkeypatch, data)
    known = datetime(2026, 7, 16, 11, tzinfo=timezone.utc)

    result = _json(main.changes(
        "neu gefasst", "fed_testg", "§ 1", None, None, False,
        known, 10, 0))
    assert result["kind"] == "lexgraph-amendment-events"
    assert result["matched"] == 1
    event = result["events"][0]
    assert event["published_at"] == "2024-12-20"
    assert event["effective_at"] == "2025-01-01"
    assert event["future"] is False
    assert event["commands"][0]["old_text_constraint"] == "Alt"

    document = _json(main.amending_act("bgbl-1-2024-999", known))
    assert document["kind"] == "lexgraph-amending-act"
    assert document["published_at"] == "2024-12-20"
    assert document["title"] == "Teständerungsgesetz"
    assert document["event_count"] == 1
    assert document["command_count"] == 1
    assert document["affected_acts"][0]["affected_norms"] == ["§ 1"]
    assert document["articles"][0]["events"][0]["act_id"] == "fed_testg"


def test_changes_support_norm_and_knowledge_time_filters(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    _configure(monkeypatch, data)
    known = datetime(2026, 7, 16, 11, tzinfo=timezone.utc)

    no_norm = _json(main.changes(
        None, None, "§ 99", None, None, None, known, 10, 0))
    assert no_norm["matched"] == 0

    with pytest.raises(HTTPException) as raised:
        main.amending_act("bgbl-1-1900-1", known)
    assert raised.value.status_code == 404


def test_global_and_per_act_atom_feeds_are_valid_and_source_linked(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    _configure(monkeypatch, data)

    global_feed = main.changes_atom(20)
    assert global_feed.media_type == "application/atom+xml"
    root = ET.fromstring(global_feed.body)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    assert len(entries) == 1
    links = entries[0].findall("a:link", ns)
    assert any(link.attrib["href"].endswith(
        "/amending-acts/bgbl-1-2024-999") for link in links)
    assert "Verkündet 2024-12-20" in entries[0].findtext(
        "a:summary", namespaces=ns)

    act_feed = main.act_changes_atom("fed_testg", 20)
    act_root = ET.fromstring(act_feed.body)
    assert act_root.findtext("a:id", namespaces=ns).endswith(
        "/acts/fed_testg/changes.atom")


def test_health_is_a_retrospective_integrity_gate(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    _configure(monkeypatch, data)

    healthy = main.health()
    assert healthy["status"] == "ok"
    assert healthy["retrospective"]["status"] == "ok"
    assert healthy["retrospective"]["built_at"] == \
        "2026-07-16T12:00:00Z"

    manifest_path = data / "retrospective_history.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["acts"]["fed_testg"]["events"][0]["date"] = "2025-01-02"
    _write(manifest_path, manifest)
    _configure(monkeypatch, data)

    with pytest.raises(HTTPException) as raised:
        main.health()
    assert raised.value.status_code == 503
    assert "integrity failure" in str(raised.value.detail)


def test_legacy_archive_survives_missing_retrospective_build(
        tmp_path: Path, monkeypatch) -> None:
    data = _data_plane(tmp_path)
    (data / "retrospective_history.json").unlink()
    _configure(monkeypatch, data)

    archive = _json(main.act_archive("fed_testg", None))
    assert archive["act_id"] == "fed_testg"
    assert archive["entries"][-1]["exact"] is True
    assert archive["retrospective"]["available"] is False

    with pytest.raises(HTTPException) as raised:
        main.act_markdown(
            "fed_testg", None,
            datetime(2026, 7, 16, 11, tzinfo=timezone.utc), None, False)
    assert raised.value.status_code == 422
