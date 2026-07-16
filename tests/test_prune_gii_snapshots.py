from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from official_states import archive_gii_states  # noqa: E402
from prune_gii_snapshots import prune_gii_snapshots  # noqa: E402


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows), encoding="utf-8")


def _snapshot(root: Path, day: str, text: str = "Text") -> Path:
    snapshot = root / day
    _jsonl(snapshot / "acts.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "norm_count": 1,
        "long_title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "builddate": f"{day.replace('-', '')}010101", "doknr": "A1",
    }])
    _jsonl(snapshot / "norms.jsonl", [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 1",
        "titel": "Zweck", "text": text, "gliederung": "Teil 1",
        "doknr": "N1",
    }])
    return snapshot


def test_prunes_only_archived_snapshots_and_keeps_newest_two(
        tmp_path: Path) -> None:
    root, store = tmp_path / "snapshots", tmp_path / "store"
    snapshots = [
        _snapshot(root, "2026-01-01", "eins"),
        _snapshot(root, "2026-01-02", "zwei"),
        _snapshot(root, "2026-01-03", "drei"),
        _snapshot(root, "2026-01-04", "vier"),
    ]
    archive_gii_states(snapshots, store)
    incomplete = root / "2025-12-30"
    _jsonl(incomplete / "acts.jsonl", [])
    manual = root / "manual-notes"
    manual.mkdir()
    invalid_date = root / "2026-99-99"
    _jsonl(invalid_date / "acts.jsonl", [])
    _jsonl(invalid_date / "norms.jsonl", [])

    result = prune_gii_snapshots(root, store)

    assert [path.name for path in result["pruned"]] == [
        "2026-01-01", "2026-01-02"]
    assert [path.name for path in result["retained"]] == [
        "2026-01-03", "2026-01-04"]
    assert not snapshots[0].exists()
    assert not snapshots[1].exists()
    assert snapshots[2].is_dir() and snapshots[3].is_dir()
    assert incomplete.is_dir()
    assert manual.is_dir()
    assert invalid_date.is_dir()


def test_missing_observation_aborts_before_any_deletion(tmp_path: Path) -> None:
    root, store = tmp_path / "snapshots", tmp_path / "store"
    snapshots = [
        _snapshot(root, "2026-01-01", "not archived"),
        _snapshot(root, "2026-01-02", "archived"),
        _snapshot(root, "2026-01-03", "archived newest"),
    ]
    archive_gii_states(snapshots[1:], store)

    with pytest.raises(ValueError, match="unarchived observations"):
        prune_gii_snapshots(root, store)

    assert all(path.is_dir() for path in snapshots)


def test_corrupt_referenced_cas_aborts_before_any_deletion(
        tmp_path: Path) -> None:
    root, store = tmp_path / "snapshots", tmp_path / "store"
    snapshots = [
        _snapshot(root, "2026-01-01", "old"),
        _snapshot(root, "2026-01-02", "middle"),
        _snapshot(root, "2026-01-03", "new"),
    ]
    manifest = archive_gii_states(snapshots, store)
    old = next(row for row in manifest["observations"]
               if row["observed_at"] == "2026-01-01")
    target = store / manifest["objects"][old["state_sha256"]]["path"]
    target.write_bytes(b"not a gzip state")

    with pytest.raises(ValueError, match="state object"):
        prune_gii_snapshots(root, store)

    assert all(path.is_dir() for path in snapshots)


def test_preserves_catalog_and_auxiliary_evidence(tmp_path: Path) -> None:
    root, store = tmp_path / "snapshots", tmp_path / "store"
    snapshots = [
        _snapshot(root, "2026-01-01", "old"),
        _snapshot(root, "2026-01-02", "middle"),
        _snapshot(root, "2026-01-03", "new"),
    ]
    archive_gii_states(snapshots, store)
    catalog = snapshots[0] / "catalog.jsonl"
    auxiliary = snapshots[0] / "retrieval.json"
    _jsonl(catalog, [{
        "id": "gii:historical", "abbrev": "historical",
        "title": "Historical catalog evidence",
        "url": "https://www.gesetze-im-internet.de/historical/",
    }])
    auxiliary.write_text('{"request":"verified"}\n', encoding="utf-8")

    result = prune_gii_snapshots(root, store)

    assert result["pruned"] == [snapshots[0]]
    assert snapshots[0].is_dir()
    assert catalog.is_file()
    assert auxiliary.is_file()
    assert not (snapshots[0] / "acts.jsonl").exists()
    assert not (snapshots[0] / "norms.jsonl").exists()


def test_provenance_mismatch_aborts_before_any_deletion(tmp_path: Path) -> None:
    root, store = tmp_path / "snapshots", tmp_path / "store"
    snapshots = [
        _snapshot(root, "2026-01-01", "old"),
        _snapshot(root, "2026-01-02", "middle"),
        _snapshot(root, "2026-01-03", "new"),
    ]
    archive_gii_states(snapshots, store)
    manifest_path = store / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    observation = next(
        row for row in manifest["observations"]
        if row["observed_at"] == "2026-01-01")
    observation.update({
        "source": "not-gii",
        "source_slug": "wrong",
        "source_doknr": "wrong",
    })
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")

    with pytest.raises(ValueError, match="unarchived observations"):
        prune_gii_snapshots(root, store)

    assert all(path.is_dir() for path in snapshots)
    assert all((path / "acts.jsonl").is_file() for path in snapshots)
    assert all((path / "norms.jsonl").is_file() for path in snapshots)


def test_rejects_non_positive_retention_without_deleting(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    snapshot = _snapshot(root, "2026-01-01")

    with pytest.raises(ValueError, match="positive integer"):
        prune_gii_snapshots(root, tmp_path / "store", keep=0)

    assert snapshot.is_dir()
