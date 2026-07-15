"""Read-only official federal history helpers shared by the CLI tools.

The GII state store records *retrieval observations*.  Those dates prove
which complete parsed text GII served, but are not legal effective dates.
Legal transition reviews are consequently returned as a separate collection
and exist only when the BGBl/DIP acceptance gate has verified them.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from common import latest_snapshot, read_jsonl  # noqa: E402
from official_states import (  # noqa: E402
    DEFAULT_STORE,
    StateStoreError,
    load_manifest,
    load_state_verified,
    transitions,
)
from official_transition_review import review_transitions  # noqa: E402


def _latest_bgbl_documents() -> list[dict[str, Any]]:
    snapshot = latest_snapshot("bgbl_documents")
    if snapshot is None or not (snapshot / "documents.jsonl").is_file():
        return []
    return list(read_jsonl(snapshot / "documents.jsonl"))


def load_official_act_history(
        jurabk: str, *, store: Path = DEFAULT_STORE,
        documents: Iterable[dict[str, Any]] | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
    """Load and integrity-check one act's observations and history.

    ``observations`` and ``transitions`` retain retrieval dates only.
    ``reviews`` is deliberately separate: a row there has independently
    verified publication/effective dates from final official sources.
    """
    manifest = load_manifest(store)
    observations = sorted(
        (dict(row) for row in manifest["observations"]
         if row["jurabk"].casefold() == jurabk.casefold()),
        key=lambda row: (
            row["observed_at"], row["builddate"], row["state_sha256"]),
    )

    # A CLI checkout/log must fail closed when any object it advertises has
    # disappeared or no longer matches its content address.
    for observation in observations:
        state = load_state_verified(store, observation["state_sha256"])
        if state["id"] != observation["act_id"] or \
                state["jurabk"] != observation["jurabk"] or \
                state["norm_count"] != observation["norm_count"]:
            raise StateStoreError(
                "official observation metadata does not match its state")

    all_transitions = transitions(manifest, store)
    act_transitions = [dict(row) for row in all_transitions
                       if row["jurabk"].casefold() == jurabk.casefold()]
    source_documents = (list(documents) if documents is not None
                        else _latest_bgbl_documents())
    reviews = [dict(row) for row in review_transitions(
        all_transitions, source_documents)
        if row["jurabk"].casefold() == jurabk.casefold()]
    return {
        "observations": observations,
        "transitions": act_transitions,
        "reviews": reviews,
    }


def exact_observed_state(
        history: dict[str, list[dict[str, Any]]], observed_at: str, *,
        store: Path = DEFAULT_STORE,
        ) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return one exact observed state, never a nearest-date substitute."""
    rows = [row for row in history["observations"]
            if row["observed_at"] == observed_at]
    digests = {row["state_sha256"] for row in rows}
    if not rows:
        return None
    if len(digests) != 1:
        raise StateStoreError(
            f"multiple unordered official states observed on {observed_at}")
    observation = sorted(rows, key=lambda row: (
        row["builddate"], row["state_sha256"]))[-1]
    state = load_state_verified(store, observation["state_sha256"])
    return state, dict(observation)


def transition_key(row: dict[str, Any]) -> tuple[str, str]:
    """Stable join key shared by an observed pair and a legal review."""
    return (str(row.get("previous_state_sha256") or ""),
            str(row.get("state_sha256") or ""))
