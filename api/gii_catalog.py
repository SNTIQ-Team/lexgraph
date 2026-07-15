"""Deterministic discovery search over the complete official GII TOC.

The catalogue is intentionally metadata-only.  A result without ``act_id``
links to gesetze-im-internet.de and must not be presented as a deeply indexed
Lexgraph act.
"""
from __future__ import annotations

from collections.abc import Iterable

from api.search_engine import normalize_search_text


def _words(value: object) -> tuple[str, ...]:
    return tuple(normalize_search_text(value).split())


def _token_matches(token: str, word: str) -> bool:
    if token == word:
        return True
    # Short abbreviation prefixes turn a 6k-row catalogue into noise (``BGB``
    # must not match the ``BGBl`` citation in thousands of titles). They
    # remain useful against the dedicated abbreviation field, handled before
    # this token phase.
    return len(token) >= 4 and word.startswith(token)


def _title_phrase_matches(title: str, needle: str) -> bool:
    """Avoid treating short abbreviations as substrings of other words.

    For example ``BGB`` must not match every ``SGB`` title. Longer natural
    language fragments keep useful infix discovery such as ``Mietrecht``.
    """
    if not title or not needle:
        return False
    if " " in needle or len(needle) >= 4:
        return needle in title
    return needle in title.split()


def _score(row: dict, query: str) -> tuple[int, list[str]]:
    needle = normalize_search_text(query)
    if not needle:
        return 0, []
    tokens = tuple(needle.split())
    values = {
        "id": normalize_search_text(row.get("id")),
        "abbrev": normalize_search_text(row.get("abbrev")),
        "jurabk": normalize_search_text(row.get("jurabk")),
        "title": normalize_search_text(row.get("title")),
    }
    aliases = tuple(value for key, value in values.items()
                    if key != "title" and value)
    title = values["title"]

    matched: list[str] = []
    for field, value in values.items():
        words = value.split()
        phrase = (_title_phrase_matches(value, needle)
                  if field == "title" else needle in value)
        if value and (phrase or all(
                any(_token_matches(token, word) for word in words)
                for token in tokens)):
            matched.append(field)

    score = 0
    if needle and needle == values["jurabk"]:
        score = 1_100
    elif needle in aliases:
        score = 1_050
    elif needle == title:
        score = 1_000
    elif len(needle) >= 2 and any(alias.startswith(needle)
                                  for alias in aliases):
        score = 900
    elif len(needle) >= 3 and title.startswith(needle):
        score = 850
    elif _title_phrase_matches(title, needle):
        score = 760
    else:
        all_words = tuple(word for value in values.values()
                          for word in value.split())
        if tokens and all(any(_token_matches(token, word)
                              for word in all_words) for token in tokens):
            exact = sum(token in all_words for token in tokens)
            score = 600 + 20 * exact

    if not score:
        return 0, []
    if row.get("act_id"):
        score += 15
    return score, matched


def search_gii_catalog(rows: Iterable[dict], query: str, limit: int = 25,
                       exclude_act_ids: set[str] | None = None
                       ) -> tuple[list[dict], int]:
    """Return ranked GII metadata matches and the unpaginated match count."""
    excluded = exclude_act_ids or set()
    hits: list[dict] = []
    for source in rows:
        if str(source.get("act_id") or "") in excluded:
            continue
        score, fields = _score(source, query)
        if not score:
            continue
        row = dict(source)
        row.update({"score": score, "matched_fields": fields,
                    "source": "gii_catalog"})
        hits.append(row)
    hits.sort(key=lambda row: (
        -int(row["score"]),
        len(str(row.get("title") or "")),
        normalize_search_text(row.get("title")),
        str(row.get("id") or ""),
    ))
    return hits[:max(0, limit)], len(hits)
