from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from export_hf import (  # noqa: E402
    _export_neuris_archive,
    _export_verified_reconstructions,
)
from official_states import archive_gii_states, store_state_object  # noqa: E402


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows), encoding="utf-8")


def _snapshot(root: Path) -> Path:
    snapshot = root / "2026-07-16"
    _jsonl(snapshot / "acts.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "norm_count": 1,
        "long_title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "builddate": "20260716010101", "doknr": "A1",
    }])
    _jsonl(snapshot / "norms.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 1",
        "titel": "Zweck", "text": "neue Fassung", "gliederung": "Teil 1",
        "doknr": "N1",
    }])
    return snapshot


def _reconstruction_fixture(tmp_path: Path) -> tuple[Path, Path, str, dict]:
    store = tmp_path / "store"
    manifest = archive_gii_states([_snapshot(tmp_path / "snapshots")], store)
    anchor = next(iter(manifest["objects"]))
    derived = {
        "id": "fed_demog", "jurabk": "DemoG", "juris": "DE",
        "title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "build": "20260716", "norm_count": 1,
        "norms": [{
            "enbez": "§ 1", "titel": "Zweck", "text": "alte Fassung",
            "glied": "Teil 1",
        }],
    }
    digest, stored = store_state_object(store, derived)
    metadata = {
        **stored, "state_sha256": digest,
        "origin": "derived_verified_reverse_replay",
        "anchor_state_sha256": anchor, "source_exact": False,
    }
    artifact = {
        "schema_version": 1,
        "kind": "lexgraph-reviewed-verified-reconstructions",
        "built_at": "2026-07-16T12:00:00+00:00",
        "state_identity": "sha256-canonical-uncompressed-json",
        "reconstructions": [{
            "id": "verified:demo", "act_id": "fed_demog",
            "jurabk": "DemoG", "state_sha256": digest,
            "anchor_state_sha256": anchor,
            "text_status": "derived_verified", "body_complete": True,
            "source_exact": False, "reverse_replay_verified": True,
            "anchor_projection_metadata_retained": True,
        }],
        "object_metadata": {digest: metadata},
    }
    artifact_path = tmp_path / "verified_reconstructions.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    return artifact_path, store, digest, artifact


def test_hf_exports_only_reviewed_derived_cas_objects(tmp_path: Path) -> None:
    artifact_path, store, digest, artifact = \
        _reconstruction_fixture(tmp_path)
    out = tmp_path / "out"
    stale = out / "federal_states" / "objects" / "stale.part"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")

    rows, objects = _export_verified_reconstructions(
        out, artifact_path, store)

    assert (rows, objects) == (1, 1)
    metadata = artifact["object_metadata"][digest]
    exported = out / "federal_states" / metadata["path"]
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == \
        metadata["gzip_sha256"]
    assert not stale.exists()
    assert (out / "verified_reconstructions.json").read_bytes() == \
        artifact_path.read_bytes()


def test_hf_rejects_derived_object_without_reviewed_origin(
        tmp_path: Path) -> None:
    artifact_path, store, digest, artifact = \
        _reconstruction_fixture(tmp_path)
    artifact["object_metadata"][digest]["origin"] = "official"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsafe derived CAS metadata"):
        _export_verified_reconstructions(
            tmp_path / "out", artifact_path, store)


def _captured_neuris_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    objects = tmp_path / "objects"
    objects.mkdir()
    source = objects / "payload.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("akn.xml", "<akomaNtoso/>")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    target = objects / f"{digest}.zip"
    source.replace(target)
    url = (
        "https://testphase.rechtsinformationen.bund.de/v1/legislation/"
        "eli/bund/bgbl-1/2020/s1/2026-01-01/1/deu/2026-01-01.zip")
    captured = {
        "event_id": "event:captured", "source": "neuris_changelog",
        "kind": "consolidation_changed", "content_url": url,
        "legal_effect": "not_asserted",
        "date_basis": (
            "retrieval_observation_and_eli_identifiers_not_legal_effect"),
        "capture_status": "captured", "content_sha256": digest,
        "content_bytes": len(payload),
        "content_object": f"neuris_objects/{digest}.zip",
        "content_media_type": "application/zip",
        "content_source_url": url,
    }
    metadata_only = {
        "event_id": "event:missing", "source": "neuris_changelog",
        "kind": "consolidation_changed", "content_url": url,
        "legal_effect": "not_asserted",
        "date_basis": (
            "retrieval_observation_and_eli_identifiers_not_legal_effect"),
        "capture_status": "http_404", "content_sha256": None,
        "content_bytes": None,
    }
    ledger = tmp_path / "neuris_archive.jsonl"
    _jsonl(ledger, [captured, metadata_only])
    return ledger, objects, target


def test_hf_neuris_export_preserves_ledger_and_excludes_partials(
        tmp_path: Path) -> None:
    ledger, objects, source = _captured_neuris_fixture(tmp_path)
    (objects / ".capture-dead.part").write_bytes(b"partial")
    out = tmp_path / "out"
    stale = out / "neuris_objects" / "stale.zip"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    rows, captured = _export_neuris_archive(out, ledger, objects)

    assert (rows, captured) == (2, 1)
    assert (out / "neuris_archive.jsonl").read_bytes() == ledger.read_bytes()
    assert (out / "neuris_objects" / source.name).read_bytes() == \
        source.read_bytes()
    assert not stale.exists()
    assert not list((out / "neuris_objects").glob("*.part"))


def test_hf_neuris_export_rejects_failed_row_with_partial_reference(
        tmp_path: Path) -> None:
    ledger, objects, _source = _captured_neuris_fixture(tmp_path)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    rows[0]["capture_status"] = "download_error"
    _jsonl(ledger, rows)

    with pytest.raises(RuntimeError, match="uncaptured NeuRIS row"):
        _export_neuris_archive(tmp_path / "out", ledger, objects)
