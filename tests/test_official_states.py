from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from official_states import (  # noqa: E402
    DATE_BASIS,
    StateStoreError,
    archive_gii_states,
    canonical_json_bytes,
    load_manifest,
    load_state_verified,
    transitions,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8")


def _snapshot(root: Path, day: str, *, text: str = "alt",
              extra_norms: list[dict] | None = None,
              norm_count: int | None = None,
              builddate: str = "20260101010101") -> Path:
    path = root / day
    norms = [{
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 1",
        "titel": "Zweck", "text": text, "doknr": "N1",
        "gliederung": "Teil 1",
    }] + list(extra_norms or [])
    _write_jsonl(path / "acts.jsonl", [{
        "slug": "demog", "jurabk": "DemoG",
        "long_title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "builddate": builddate, "doknr": "A1",
        "norm_count": len(norms) if norm_count is None else norm_count,
    }])
    _write_jsonl(path / "norms.jsonl", norms)
    return path


def _object_path(store: Path, digest: str) -> Path:
    return (store / "objects" / "sha256" / digest[:2] /
            f"{digest}.json.gz")


def test_cas_is_deterministic_deduplicated_and_manifest_is_cumulative(
        tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    first = _snapshot(snapshots, "2026-07-14")
    second = _snapshot(snapshots, "2026-07-15")

    manifest = archive_gii_states([second, first], store)
    assert len(manifest["observations"]) == 2
    assert len(manifest["objects"]) == 1
    digest = manifest["observations"][0]["state_sha256"]
    assert manifest["observations"][1]["state_sha256"] == digest

    object_path = _object_path(store, digest)
    compressed = object_path.read_bytes()
    canonical = gzip.decompress(compressed)
    assert compressed[4:8] == b"\x00\x00\x00\x00"
    assert hashlib.sha256(canonical).hexdigest() == digest
    state = load_state_verified(store, digest)
    assert state == {
        "id": "fed_demog", "jurabk": "DemoG", "juris": "DE",
        "title": "Demonstrationsgesetz", "stand": "Stand: Test",
        "build": "20260101", "norm_count": 1,
        "norms": [{"enbez": "§ 1", "titel": "Zweck",
                   "text": "alt", "glied": "Teil 1"}],
    }
    assert canonical == canonical_json_bytes(state)

    manifest_bytes = (store / "manifest.json").read_bytes()
    archive_gii_states([first, second], store)
    assert (store / "manifest.json").read_bytes() == manifest_bytes

    # Losing the source snapshots cannot prune prior observations or states.
    for path in (first, second):
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    preserved = archive_gii_states([], store)
    assert preserved == manifest
    assert load_manifest(store) == manifest
    assert object_path.is_file()


def test_transitions_include_complete_add_delete_replace_norm_diffs(
        tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    deleted = {
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 2",
        "titel": "Entfällt", "text": "weg", "doknr": "N2",
        "gliederung": "Teil 1",
    }
    added = {
        "slug": "demog", "jurabk": "DemoG", "enbez": "§ 3",
        "titel": "Neu", "text": "hinzu", "doknr": "N3",
        "gliederung": "Teil 2",
    }
    old = _snapshot(snapshots, "2026-07-13", text="alt",
                    extra_norms=[deleted], builddate="20260701010101")
    unchanged = _snapshot(snapshots, "2026-07-14", text="alt",
                          extra_norms=[deleted],
                          builddate="20260701010101")
    new = _snapshot(snapshots, "2026-07-15", text="neu",
                    extra_norms=[added], builddate="20260715010101")

    manifest = archive_gii_states([new, old, unchanged], store)
    rows = transitions(manifest, store)
    assert len(rows) == 1
    row = rows[0]
    assert row["act_id"] == "fed_demog"
    assert row["date"] == row["observed_at"] == "2026-07-15"
    assert row["previous_observed_at"] == "2026-07-14"
    assert row["old_builddate"] == "20260701010101"
    assert row["new_builddate"] == "20260715010101"
    assert row["date_basis"] == DATE_BASIS
    assert row["full_state_pair"] is True
    assert row["effective_at"] is None

    changes = {change["operation"]: change for change in row["changes"]}
    assert set(changes) == {"add", "delete", "replace"}
    assert changes["replace"] | {
        "para": "§ 1", "old": "alt", "new": "neu",
        "old_present": True, "new_present": True,
    } == changes["replace"]
    assert changes["delete"]["para"] == "§ 2"
    assert changes["delete"]["old"] == "weg"
    assert changes["delete"]["new"] == ""
    assert changes["delete"]["old_present"] is True
    assert changes["delete"]["new_present"] is False
    assert changes["add"]["para"] == "§ 3"
    assert changes["add"]["old"] == ""
    assert changes["add"]["new"] == "hinzu"
    assert changes["add"]["old_present"] is False
    assert changes["add"]["new_present"] is True
    for change in changes.values():
        assert len(change["old_sha256"]) == 64
        assert len(change["new_sha256"]) == 64


def test_incomplete_norm_count_fails_without_replacing_manifest(
        tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    good = _snapshot(snapshots, "2026-07-14")
    archive_gii_states([good], store)
    before = (store / "manifest.json").read_bytes()
    bad = _snapshot(snapshots, "2026-07-15", norm_count=2)

    with pytest.raises(StateStoreError, match="expected 2 norms, captured 1"):
        archive_gii_states([bad], store)
    assert (store / "manifest.json").read_bytes() == before


def test_corrupt_or_misaddressed_object_fails_closed(tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    manifest = archive_gii_states(
        [_snapshot(snapshots, "2026-07-15")], store)
    digest = manifest["observations"][0]["state_sha256"]
    state = load_state_verified(store, digest)
    state["norms"][0]["text"] = "tampered"
    canonical = canonical_json_bytes(state)
    # A valid deterministic gzip under the old address must still fail its
    # canonical uncompressed SHA-256 check.
    target = _object_path(store, digest)
    target.write_bytes(gzip.compress(canonical, compresslevel=9, mtime=0))

    with pytest.raises(StateStoreError, match="hash mismatch"):
        load_state_verified(store, digest)
    with pytest.raises(StateStoreError):
        archive_gii_states([], store)


def test_duplicate_norm_labels_match_unchanged_state_before_deletion(
        tmp_path: Path) -> None:
    snapshots, store = tmp_path / "snapshots", tmp_path / "store"
    retained = {
        "slug": "demog", "jurabk": "DemoG", "enbez": "Anlage",
        "titel": "(zu § 2)", "text": "bleibt", "doknr": "N3",
        "gliederung": "Anlagen",
    }
    removed = retained | {
        "titel": "(zu § 1)", "text": "entfällt", "doknr": "N2",
    }
    old = _snapshot(snapshots, "2026-07-14", extra_norms=[removed, retained],
                    builddate="20260714010101")
    new = _snapshot(snapshots, "2026-07-15", extra_norms=[retained],
                    builddate="20260715010101")

    rows = transitions(archive_gii_states([old, new], store), store)
    assert len(rows) == 1
    assert [(change["operation"], change["para"], change["old"])
            for change in rows[0]["changes"]] == [
                ("delete", "Anlage", "entfällt")]
