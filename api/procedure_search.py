"""Small deterministic search over official German and EU procedures.

The procedure catalogue is already refreshed from the official DIP endpoint.
This module searches the compact rows embedded in ``hierarchy.json``; it does
not infer a legislative stage or claim that a proposal is in force.
"""
from __future__ import annotations

from collections.abc import Iterable

from api.search_engine import normalize_search_text


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item


def _match_score(row: dict, query: str) -> tuple[int, list[str]]:
    needle = normalize_search_text(query)
    if not needle:
        return 0, []
    tokens = needle.split()
    fields: tuple[tuple[str, object, int], ...] = (
        ("identifier", [row.get("id"), row.get("procedure"),
                        row.get("gesta"), row.get("proposal_celex")], 700),
        ("title", row.get("title"), 600),
        ("descriptor", row.get("descriptors"), 520),
        ("watch", (row.get("watch") or {}).get("queries"), 500),
        ("scope", (row.get("watch") or {}).get("scope"), 440),
        ("topic", row.get("topics"), 360),
        ("initiator", row.get("initiators"), 280),
        ("summary", row.get("summary"), 200),
    )
    score = 0
    matched: list[str] = []
    for name, value, weight in fields:
        candidates = [normalize_search_text(item) for item in _strings(value)]
        candidates = [item for item in candidates if item]
        if not candidates:
            continue
        phrase = any(needle in item for item in candidates)
        token_match = all(any(token in item for item in candidates)
                          for token in tokens)
        if phrase or token_match:
            score += weight + (80 if phrase else 0)
            matched.append(name)
    if row.get("watched") and score:
        score += 75
    return score, matched


def search_procedures(hierarchy: object, query: str,
                      limit: int = 20) -> list[dict]:
    """Return DIP/EUR-Lex rows ranked without changing official stage data."""
    if not isinstance(hierarchy, dict):
        return []
    rows: list[tuple[dict, str]] = []
    for lane, default_source in (("bund", "DIP"), ("eu", "EUR-Lex")):
        pipeline = (hierarchy.get(lane) or {}).get("pipeline") or {}
        groups = pipeline.values() if isinstance(pipeline, dict) else [pipeline]
        rows.extend((row, default_source)
                    for group in groups if isinstance(group, list)
                    for row in group if isinstance(row, dict))
    hits: list[dict] = []
    for row, default_source in rows:
        score, fields = _match_score(row, query)
        if not score:
            continue
        hit = dict(row)
        hit["score"] = score
        hit["matched_fields"] = fields
        hit["source"] = row.get("source") or default_source
        hits.append(hit)
    def date_rank(row: dict) -> int:
        digits = "".join(char for char in str(row.get("date") or "")
                         if char.isdigit())[:8]
        return int(digits) if digits else 0

    hits.sort(key=lambda row: (
        -int(row.get("score") or 0),
        -date_rank(row),
        str(row.get("id") or ""),
    ))
    return hits[:max(0, limit)]
