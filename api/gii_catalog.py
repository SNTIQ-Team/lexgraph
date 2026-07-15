"""Deterministic discovery search over the complete official GII TOC.

The catalogue is intentionally metadata-only.  A result without ``act_id``
links to gesetze-im-internet.de and must not be presented as a deeply indexed
Lexgraph act.
"""
from __future__ import annotations

from collections.abc import Iterable

from api.search_engine import normalize_search_text


# A few statutes are overwhelmingly searched by their established short
# title, while GII's table of contents exposes only the formal long title.
# Keep these aliases deliberately small and reviewable; they improve discovery
# without pretending that the alias came from the official TOC metadata.
COMMON_TITLE_ALIASES = {
    "burlg": ("Bundesurlaubsgesetz",),
}


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


def _normalized_values(row: dict) -> dict[str, str]:
    values = {
        "id": normalize_search_text(row.get("id")),
        "abbrev": normalize_search_text(row.get("abbrev")),
        "jurabk": normalize_search_text(row.get("jurabk")),
        "title": normalize_search_text(row.get("title")),
    }
    aliases = COMMON_TITLE_ALIASES.get(values["abbrev"], ())
    values["alias"] = " ".join(
        normalize_search_text(alias) for alias in aliases)
    return values


def _score_values(row: dict, values: dict[str, str],
                  needle: str) -> tuple[int, list[str]]:
    if not needle:
        return 0, []
    tokens = tuple(needle.split())
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


def _score(row: dict, query: str) -> tuple[int, list[str]]:
    return _score_values(
        row, _normalized_values(row), normalize_search_text(query))


class GiiCatalogIndex:
    """Pre-normalized in-memory index for the official 6k-row GII TOC.

    Unicode folding every title for every keystroke cost several seconds on
    the small production VPS.  The catalogue is immutable for one API process,
    so doing that work once at startup makes each search a cheap deterministic
    scan while preserving the exact ranking contract.
    """

    def __init__(self, rows: Iterable[dict]):
        self.source_rows = rows
        self.rows = tuple(
            (source, _normalized_values(source)) for source in rows)
        self.gram_rows: dict[str, set[int]] = {}
        self.exact_word_rows: dict[str, set[int]] = {}
        self.alias_prefix_rows: dict[str, set[int]] = {}
        for position, (_, values) in enumerate(self.rows):
            for value in values.values():
                for word in value.split():
                    self.exact_word_rows.setdefault(word, set()).add(position)
                # Three-character postings preserve infix/phrase semantics
                # while reducing a query to a few dozen candidate rows.
                for offset in range(max(0, len(value) - 2)):
                    gram = value[offset:offset + 3]
                    self.gram_rows.setdefault(gram, set()).add(position)
            # The ranking contract permits two-character prefixes only for
            # dedicated identifiers/abbreviations, never arbitrary titles.
            for field in ("id", "abbrev", "jurabk", "alias"):
                value = values[field]
                for width in range(2, len(value) + 1):
                    self.alias_prefix_rows.setdefault(
                        value[:width], set()).add(position)

    def _candidate_positions(self, needle: str) -> set[int]:
        per_token: list[set[int]] = []
        for token in needle.split():
            if len(token) >= 3:
                postings = [self.gram_rows.get(token[offset:offset + 3], set())
                            for offset in range(len(token) - 2)]
                candidates = (set.intersection(*postings)
                              if postings and all(postings) else set())
            else:
                candidates = set(self.exact_word_rows.get(token, set()))
                if len(token) >= 2:
                    candidates.update(
                        self.alias_prefix_rows.get(token, set()))
            if not candidates:
                return set()
            per_token.append(candidates)
        return set.intersection(*per_token) if per_token else set()

    def search(self, query: str, limit: int = 25,
               exclude_act_ids: set[str] | None = None
               ) -> tuple[list[dict], int]:
        needle = normalize_search_text(query)
        excluded = exclude_act_ids or set()
        hits: list[tuple[dict, str]] = []
        for position in self._candidate_positions(needle):
            source, values = self.rows[position]
            if str(source.get("act_id") or "") in excluded:
                continue
            score, fields = _score_values(source, values, needle)
            if not score:
                continue
            row = dict(source)
            row.update({"score": score, "matched_fields": fields,
                        "source": "gii_catalog"})
            hits.append((row, values["title"]))
        hits.sort(key=lambda item: (
            -int(item[0]["score"]),
            len(str(item[0].get("title") or "")),
            item[1],
            str(item[0].get("id") or ""),
        ))
        return [row for row, _ in hits[:max(0, limit)]], len(hits)


def search_gii_catalog(rows: Iterable[dict], query: str, limit: int = 25,
                       exclude_act_ids: set[str] | None = None
                       ) -> tuple[list[dict], int]:
    """Return ranked GII metadata matches and the unpaginated match count."""
    return GiiCatalogIndex(rows).search(
        query, limit=limit, exclude_act_ids=exclude_act_ids)
