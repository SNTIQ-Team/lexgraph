from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.official_state_store import (
    OfficialStateError,
    clear_cache,
    load_observed_state,
    observation_on,
)


def _write_state(root: Path, state: dict) -> str:
    payload = json.dumps(
        state, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = (root / "federal_states" / "objects" / "sha256" / digest[:2]
            / f"{digest}.json.gz")
    path.parent.mkdir(parents=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
            handle.write(payload)
    return digest


def _act(digest: str) -> dict:
    return {
        "id": "fed_testg", "jurabk": "TestG",
        "official_states": [{
            "observed_at": "2026-07-13",
            "state_sha256": digest,
            "norm_count": 1,
            "builddate": "20260712010101",
            "source_url": "https://www.gesetze-im-internet.de/testg/",
            "date_basis": "retrieval_observation_not_effective_date",
        }],
    }


def test_resolves_only_exact_observation_and_verifies_content(tmp_path: Path):
    state = {"schema_version": 1, "act_id": "fed_testg",
             "jurabk": "TestG",
             "norms": [{"enbez": "§ 1", "text": "amtlich"}]}
    digest = _write_state(tmp_path, state)
    act = _act(digest)

    assert observation_on(act, "2026-07-12") is None
    assert load_observed_state(tmp_path, act, "2026-07-12") is None
    loaded = load_observed_state(tmp_path, act, "2026-07-13")
    assert loaded is not None
    assert loaded["norms"][0]["text"] == "amtlich"
    assert loaded["state_sha256"] == digest
    assert loaded["date_basis"] == \
        "retrieval_observation_not_effective_date"


def test_hash_tampering_fails_closed(tmp_path: Path):
    state = {"act_id": "fed_testg", "jurabk": "TestG",
             "norms": [{"enbez": "§ 1", "text": "amtlich"}]}
    digest = _write_state(tmp_path, state)
    path = (tmp_path / "federal_states" / "objects" / "sha256"
            / digest[:2] / f"{digest}.json.gz")
    clear_cache()
    with gzip.open(path, "wb") as handle:
        handle.write(b'{"norms":[]}')
    with pytest.raises(OfficialStateError, match="hash mismatch"):
        load_observed_state(tmp_path, _act(digest), "2026-07-13")
