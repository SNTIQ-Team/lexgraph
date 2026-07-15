"""Read-only resolver for published content-addressed federal states.

The pipeline hashes the canonical, uncompressed JSON bytes and publishes the
same bytes as deterministic gzip objects.  The API verifies that digest again
before trusting a historical body.  An observation is resolved only for its
exact retrieval date: choosing the latest earlier observation would silently
turn a crawl timestamp into a legal point-in-time assertion.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


class OfficialStateError(ValueError):
    """A published observation/object pair failed its integrity contract."""


_DIGEST = re.compile(r"[0-9a-f]{64}")


def observation_on(act: dict[str, Any], requested_at: str | None
                   ) -> dict[str, Any] | None:
    """Return the one exact official retrieval observation for a date."""
    if not requested_at:
        return None
    matches = []
    for row in act.get("official_states") or []:
        if not isinstance(row, dict):
            continue
        observed = str(row.get("observed_at") or row.get("date") or "")[:10]
        digest = str(row.get("state_sha256") or row.get("state_digest") or "")
        if observed == requested_at and _DIGEST.fullmatch(digest):
            matches.append({**row, "observed_at": observed,
                            "state_sha256": digest})
    if not matches:
        return None
    return max(matches, key=lambda row: (
        str(row.get("builddate") or ""), row["state_sha256"]))


@lru_cache(maxsize=256)
def _read_object(path_string: str, digest: str) -> dict[str, Any]:
    path = Path(path_string)
    try:
        with gzip.open(path, "rb") as handle:
            payload = handle.read()
    except (OSError, EOFError) as exc:
        raise OfficialStateError(f"cannot read official state object {digest}") \
            from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise OfficialStateError(
            f"official state object hash mismatch: expected {digest}, got {actual}")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialStateError(
            f"official state object {digest} is not canonical JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("norms"), list):
        raise OfficialStateError(
            f"official state object {digest} has no norm list")
    return value


def load_observed_state(data_dir: Path, act: dict[str, Any],
                        requested_at: str | None
                        ) -> dict[str, Any] | None:
    """Load and verify a complete state captured on exactly requested_at."""
    observation = observation_on(act, requested_at)
    if observation is None:
        return None
    digest = observation["state_sha256"]
    path = (data_dir / "federal_states" / "objects" / "sha256"
            / digest[:2] / f"{digest}.json.gz")
    if not path.is_file():
        raise OfficialStateError(
            f"official state object is missing for observation {requested_at}")
    state = _read_object(str(path), digest)
    expected_act_id = str(act.get("id") or "")
    expected_jurabk = str(act.get("jurabk") or "")
    object_act_id = state.get("act_id") or state.get("id")
    if object_act_id != expected_act_id or str(
            state.get("jurabk") or "") != expected_jurabk:
        raise OfficialStateError(
            f"official state object {digest} belongs to another act")
    expected_count = observation.get("norm_count")
    if expected_count is not None and len(state["norms"]) != int(expected_count):
        raise OfficialStateError(
            f"official state object {digest} has an unexpected norm count")
    return {
        **state,
        **observation,
        "source": observation.get("source") or "GII",
        "date_basis": observation.get("date_basis")
        or "retrieval_observation_not_effective_date",
        "verification": observation.get("verification") or "exact",
    }


def load_state_digest(state_root: Path, digest: str, *,
                      act_id: str | None = None,
                      jurabk: str | None = None) -> dict[str, Any]:
    """Load one immutable state by digest for bitemporal history queries.

    ``state_root`` is the directory containing ``objects/sha256``.  Unlike
    :func:`load_observed_state`, this lookup is anchored by a separately
    validated legal interval rather than by a retrieval-day observation.
    Optional act identities prevent a valid object from being attached to the
    wrong retrospective interval.
    """
    if not _DIGEST.fullmatch(str(digest or "")):
        raise OfficialStateError("official state digest is invalid")
    path = (Path(state_root) / "objects" / "sha256" / digest[:2]
            / f"{digest}.json.gz")
    if not path.is_file():
        raise OfficialStateError(
            f"official state object is missing for digest {digest}")
    state = _read_object(str(path), digest)
    object_act_id = str(state.get("act_id") or state.get("id") or "")
    object_jurabk = str(state.get("jurabk") or "")
    if act_id is not None and object_act_id != act_id:
        raise OfficialStateError(
            f"official state object {digest} belongs to another act")
    if jurabk is not None and object_jurabk != jurabk:
        raise OfficialStateError(
            f"official state object {digest} has another jurabk")
    return state


def clear_cache() -> None:
    """Test/deployment hook; generations are otherwise immutable."""
    _read_object.cache_clear()
