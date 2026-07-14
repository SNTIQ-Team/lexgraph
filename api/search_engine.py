"""Deterministic full-text search for the static Lexgraph data plane.

The build step writes ``search.sqlite`` next to the other ``web/data``
artifacts.  The API opens it read-only and lets SQLite FTS5 do the candidate
selection/ranking; no network service, embedding model, or mutable runtime
index is involved.

Searchable fields are folded with Unicode NFKD, case folding, German ``ss``
handling, and diacritic removal.  Display fields retain the official spelling.
Synonyms live in ``data/search_synonyms.json`` and are embedded into the
database, so a deployed index is self-contained and reproducible.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "2"
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
NORM_REF_RE = re.compile(
    r"(?P<kind>§+|\bArt(?:ikel)?\.?)[\s\u00a0]*"
    r"(?P<number>\d+[a-z]?)",
    flags=re.IGNORECASE,
)
# Repeated concept tokens encode the curated 1..5 target priority.  A step
# deliberately outweighs a coincidental literal body match, so a Ukraine
# query surfaces the controlling/benefit norms before raw mentions.
CONCEPT_PRIORITY_STEP = 60


def normalize_search_text(value: object) -> str:
    """Fold text for matching while retaining non-Latin scripts.

    Examples: ``Über`` -> ``uber``, ``Straße`` -> ``strasse``.  Cyrillic and
    Ukrainian letters remain searchable rather than being transliterated.
    """
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    out: list[str] = []
    latin_base = False
    for char in text:
        if unicodedata.combining(char):
            # Fold Latin accents/umlauts for German search, but do not erase
            # script-significant marks such as Ukrainian ї.
            if not latin_base:
                out.append(char)
            continue
        if char.isalnum():
            out.append(char)
            latin_base = "LATIN" in unicodedata.name(char, "")
        else:
            out.append(" ")
            latin_base = False
    folded = unicodedata.normalize("NFC", "".join(out))
    return " ".join(folded.split())


def _tokens(value: object) -> list[str]:
    return TOKEN_RE.findall(normalize_search_text(value))


def _roman_number(value: int) -> str:
    """Return a compact Roman numeral for statute-book aliases."""
    numerals = (
        (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"),
        (4, "iv"), (1, "i"),
    )
    out = []
    for number, numeral in numerals:
        while value >= number:
            out.append(numeral)
            value -= number
    return "".join(out)


def _jurabk_aliases(value: object) -> set[tuple[str, ...]]:
    """Build exact token aliases for an official act abbreviation.

    The corpus spells the social-code books as ``SGB 2`` while users often
    type ``SGB II``.  A trailing edition year is also optional when it does
    not make the alias ambiguous (for example ``AufenthG 2004``).
    """
    tokens = tuple(_tokens(value))
    if not tokens:
        return set()
    aliases = {tokens}
    if (len(tokens) > 1 and tokens[-1].isdigit()
            and 1800 <= int(tokens[-1]) <= 2200):
        aliases.add(tokens[:-1])
    for alias in tuple(aliases):
        for position, token in enumerate(alias):
            if token.isdigit() and 1 <= int(token) <= 50:
                roman = list(alias)
                roman[position] = _roman_number(int(token))
                aliases.add(tuple(roman))
    return aliases


def _read_synonyms(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    groups = payload.get("groups") or []
    clean: list[dict] = []
    for pos, group in enumerate(groups):
        group_id = str(group.get("id") or f"group-{pos + 1}")
        terms = sorted({normalize_search_text(term)
                        for term in group.get("terms") or []
                        if normalize_search_text(term)})
        prefixes = sorted({normalize_search_text(prefix)
                           for prefix in group.get("prefixes") or []
                           if normalize_search_text(prefix)})
        if terms or prefixes:
            clean.append({"id": group_id, "terms": terms,
                          "prefixes": prefixes,
                          "targets": group.get("targets") or {}})
    return clean


def build_search_database(details: dict[str, dict], output: Path,
                          synonyms_path: Path) -> dict[str, int]:
    """Build an atomic FTS5 index from ``build_wiki`` act details.

    ``details`` is keyed by stable act id and already contains every current
    norm.  Inserting in sorted order makes row ids and result tie-breaking
    stable across rebuilds from the same snapshots.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.unlink(missing_ok=True)
    synonyms = _read_synonyms(synonyms_path)
    # Curated semantic links must never disappear silently after an act-id or
    # norm-label change.  Fail the deterministic build with a useful message.
    missing_targets: list[str] = []
    norm_refs = {
        act_id: {str(norm.get("enbez") or "")
                 for norm in act.get("norms") or []}
        for act_id, act in details.items()
    }
    for group in synonyms:
        targets = group["targets"]
        act_targets = (list(targets.get("acts") or []) +
                       list(targets.get("norm_acts") or []))
        for act_id in act_targets:
            if str(act_id) not in details:
                missing_targets.append(f"{group['id']}: act {act_id}")
        for norm in targets.get("norms") or []:
            act_id = str(norm.get("act_id") or "")
            enbez = str(norm.get("enbez") or "")
            if act_id not in details or enbez not in norm_refs.get(act_id, set()):
                missing_targets.append(
                    f"{group['id']}: norm {act_id} {enbez}")
    if missing_targets:
        raise ValueError("missing search synonym target(s): " +
                         "; ".join(missing_targets))
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            CREATE TABLE search_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE search_synonym (
                group_id TEXT NOT NULL,
                value TEXT NOT NULL,
                is_prefix INTEGER NOT NULL CHECK (is_prefix IN (0, 1)),
                PRIMARY KEY (group_id, value, is_prefix)
            ) WITHOUT ROWID;
            CREATE VIRTUAL TABLE search_fts USING fts5(
                kind UNINDEXED,
                act_id UNINDEXED,
                juris UNINDEXED,
                source UNINDEXED,
                jurabk_n,
                act_title_n,
                norm_ref_n,
                norm_title_n,
                body_n,
                concepts_n,
                jurabk UNINDEXED,
                act_title UNINDEXED,
                enbez UNINDEXED,
                norm_title UNINDEXED,
                body UNINDEXED,
                tokenize = 'unicode61 remove_diacritics 2',
                prefix = '2 3 4'
            );
        """)
        conn.executemany(
            "INSERT INTO search_meta(key, value) VALUES (?, ?)",
            [("schema_version", SCHEMA_VERSION),
             ("synonyms_source", synonyms_path.name)])
        synonym_rows = []
        act_concepts: dict[str, set[str]] = {}
        norm_act_concepts: dict[str, set[str]] = {}
        norm_concepts: dict[tuple[str, str], list[str]] = {}
        for group in synonyms:
            synonym_rows.extend((group["id"], term, 0)
                                for term in group["terms"])
            synonym_rows.extend((group["id"], prefix, 1)
                                for prefix in group["prefixes"])
            concept = "syn" + normalize_search_text(group["id"]).replace(
                " ", "")
            targets = group["targets"]
            for act_id in targets.get("acts") or []:
                act_concepts.setdefault(str(act_id), set()).add(concept)
            for act_id in targets.get("norm_acts") or []:
                norm_act_concepts.setdefault(str(act_id), set()).add(concept)
            for norm in targets.get("norms") or []:
                act_id = str(norm.get("act_id") or "")
                enbez = str(norm.get("enbez") or "")
                if act_id and enbez:
                    weight = max(1, min(5, int(norm.get("weight") or 1)))
                    norm_concepts.setdefault((act_id, enbez), []).extend(
                        [concept] * weight)
        conn.executemany(
            "INSERT INTO search_synonym(group_id, value, is_prefix) "
            "VALUES (?, ?, ?)", synonym_rows)

        insert = """INSERT INTO search_fts(
            kind, act_id, juris, source,
            jurabk_n, act_title_n, norm_ref_n, norm_title_n, body_n, concepts_n,
            jurabk, act_title, enbez, norm_title, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        act_count = norm_count = 0
        for act_id in sorted(details):
            act = details[act_id]
            juris = str(act.get("juris") or "")
            source = "gii" if juris == "DE" else "bayern_recht"
            jurabk = str(act.get("jurabk") or "")
            act_title = str(act.get("title") or jurabk)
            conn.execute(insert, (
                "act", act_id, juris, source,
                normalize_search_text(f"{act_id} {jurabk}"),
                normalize_search_text(act_title), "", "", "",
                " ".join(sorted(act_concepts.get(act_id, set()))),
                jurabk, act_title, "", "", ""))
            act_count += 1
            norms = sorted(
                act.get("norms") or [],
                key=lambda n: (str(n.get("enbez") or ""),
                               str(n.get("titel") or "")))
            for norm in norms:
                enbez = str(norm.get("enbez") or "")
                norm_title = str(norm.get("titel") or "")
                body = str(norm.get("text") or "")
                conn.execute(insert, (
                    "norm", act_id, juris, source,
                    normalize_search_text(jurabk),
                    normalize_search_text(act_title),
                    normalize_search_text(enbez),
                    normalize_search_text(norm_title),
                    normalize_search_text(body),
                    " ".join(sorted(
                        list(norm_act_concepts.get(act_id, set())) +
                        norm_concepts.get((act_id, enbez), []))),
                    jurabk, act_title, enbez, norm_title, body))
                norm_count += 1
        conn.executemany(
            "INSERT INTO search_meta(key, value) VALUES (?, ?)",
            [("acts", str(act_count)), ("norms", str(norm_count))])
        conn.commit()
        # Force all index pages into the main file before the atomic replace.
        conn.execute("PRAGMA optimize")
        conn.commit()
    except Exception:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    else:
        conn.close()
    os.replace(tmp, output)
    return {"acts": act_count, "norms": norm_count}


def _quote_fts(value: str, *, prefix: bool = False) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"' + ("*" if prefix else "")


def _concept_token(group_id: str) -> str:
    return "syn" + normalize_search_text(group_id).replace(" ", "")


class SearchEngine:
    """Read-only query facade over a built ``search.sqlite`` artifact."""

    def __init__(self, path: Path, wiki: Iterable[dict]):
        self.path = path
        self.wiki = {str(row.get("id")): row for row in wiki}
        uri = f"file:{path.resolve()}?mode=ro&immutable=1"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        version = self.conn.execute(
            "SELECT value FROM search_meta WHERE key='schema_version'"
        ).fetchone()
        if not version or version[0] != SCHEMA_VERSION:
            self.conn.close()
            raise RuntimeError("unsupported Lexgraph search index schema")
        self.synonym_groups = self._load_synonyms()
        self.act_aliases = self._load_act_aliases()

    def close(self) -> None:
        self.conn.close()

    def _load_synonyms(self) -> list[dict]:
        groups: dict[str, dict[str, set[str]]] = {}
        for row in self.conn.execute(
                "SELECT group_id, value, is_prefix FROM search_synonym "
                "ORDER BY group_id, is_prefix, value"):
            group = groups.setdefault(row["group_id"],
                                      {"terms": set(), "prefixes": set()})
            group["prefixes" if row["is_prefix"] else "terms"].add(
                row["value"])
        return [{"id": key, "concept": _concept_token(key),
                 "terms": sorted(value["terms"]),
                 "prefixes": sorted(value["prefixes"])}
                for key, value in sorted(groups.items())]

    def _load_act_aliases(self) -> dict[tuple[str, ...], set[str]]:
        aliases: dict[tuple[str, ...], set[str]] = {}
        for act_id, row in self.wiki.items():
            for alias in _jurabk_aliases(row.get("jurabk")):
                aliases.setdefault(alias, set()).add(act_id)
        return aliases

    def _act_reference_constraint(
            self, query: str) -> tuple[set[str] | None, set[int]]:
        """Resolve a unique act abbreviation next to an explicit norm ref.

        Returns both the exact act id and the matched token positions.  Those
        alias tokens are removed from the FTS expression because the SQL act
        id constraint is stronger and avoids prefix accidents such as
        ``II`` matching the ``III`` in an SGB III title.
        """
        if not NORM_REF_RE.search(query):
            return None, set()
        tokens = _tokens(query)
        matches: list[tuple[int, int, str]] = []
        for alias, act_ids in self.act_aliases.items():
            if len(act_ids) != 1 or len(alias) > len(tokens):
                continue
            act_id = next(iter(act_ids))
            width = len(alias)
            for start in range(len(tokens) - width + 1):
                if tuple(tokens[start:start + width]) == alias:
                    matches.append((width, start, act_id))
        if not matches:
            return None, set()
        longest = max(width for width, _, _ in matches)
        best = [match for match in matches if match[0] == longest]
        act_ids = {act_id for _, _, act_id in best}
        if len(act_ids) != 1:
            return None, set()
        width, start, act_id = min(best, key=lambda item: item[1])
        return {act_id}, set(range(start, start + width))

    def _concepts_for(self, value: str, *, whole: bool = False) -> set[str]:
        concepts = set()
        for group in self.synonym_groups:
            matched = value in group["terms"] or (not whole and any(
                value.startswith(prefix) for prefix in group["prefixes"]))
            if matched:
                concepts.add(group["concept"])
        return concepts

    def _match_expression(self, query: str, *, norm: bool = False,
                          skip_token_indexes: set[int] | None = None
                          ) -> tuple[str, list[str], set[str]]:
        tokens = _tokens(query)
        explicit_refs: dict[str, set[str]] = {}
        for match in NORM_REF_RE.finditer(query):
            number = normalize_search_text(match.group("number"))
            marker = match.group("kind").casefold()
            ref = number if marker.startswith("§") else f"art {number}"
            explicit_refs.setdefault(number, set()).add(ref)
        clauses = []
        snippet_terms: set[str] = set(tokens)
        matched_concepts: set[str] = set()
        for position, token in enumerate(tokens):
            if skip_token_indexes and position in skip_token_indexes:
                continue
            # Section numbers are identifiers: § 24 must not expand to § 241,
            # § 249a, or § 24b.  Word prefixes remain useful for inflection
            # and concatenated German abbreviations.
            if norm and token in explicit_refs:
                alternatives = {"norm_ref_n:" + _quote_fts(ref, prefix=False)
                                for ref in explicit_refs[token]}
            else:
                alternatives = {
                    _quote_fts(token, prefix=not token.isdigit())}
            concepts = self._concepts_for(token)
            matched_concepts.update(concepts)
            alternatives.update(
                "concepts_n:" + _quote_fts(concept)
                for concept in concepts)
            clauses.append("(" + " OR ".join(sorted(alternatives)) + ")")
        expression = " AND ".join(clauses)
        # A multi-word synonym is one concept, not two unrelated token
        # expansions.  Recognise the whole normalized query and OR its curated
        # concept targets with the ordinary lexical expression.
        whole = normalize_search_text(query)
        whole_concepts = self._concepts_for(whole, whole=True)
        if expression and whole_concepts:
            matched_concepts.update(whole_concepts)
            concept_clause = " OR ".join(
                "concepts_n:" + _quote_fts(concept)
                for concept in sorted(whole_concepts))
            expression = f"(({expression}) OR ({concept_clause}))"
        return (expression,
                sorted(snippet_terms, key=lambda s: (-len(s), s)),
                matched_concepts)

    @staticmethod
    def _matched_fields(row: sqlite3.Row, terms: list[str],
                        kind: str,
                        concepts: set[str] | None = None) -> list[str]:
        fields = [("jurabk", row["jurabk"]),
                  ("act_title", row["act_title"])]
        if kind == "norm":
            fields.extend((("enbez", row["enbez"]),
                           ("norm_title", row["norm_title"]),
                           ("text", row["body"])))
        matched = []
        for name, value in fields:
            folded = normalize_search_text(value)
            if any(term and term in folded for term in terms):
                matched.append(name)
        if concepts and concepts.intersection(
                str(row["concepts_n"] or "").split()):
            matched.append("concept")
        return matched

    @staticmethod
    def _relevance(row: sqlite3.Row, fields: list[str], query: str,
                   kind: str, concepts: set[str] | None = None) -> int:
        """Deterministic field-aware boost on top of FTS candidate ranking.

        For norm results, a hit in the norm heading/body is deliberately more
        useful than merely inheriting a matching act title.  This prevents
        empty Schlussformel rows from outranking e.g. AufenthG § 24 for a
        temporary-protection query.
        """
        weights = ({"jurabk": 24, "act_title": 14, "concept": 42}
                   if kind == "act" else
                   {"jurabk": 5, "act_title": 2, "enbez": 30,
                    "norm_title": 24, "text": 20, "concept": 40})
        score = sum(weights.get(field, 0) for field in fields)
        if "concept" in fields and concepts:
            tagged = str(row["concepts_n"] or "").split()
            repeats = sum(tagged.count(concept) for concept in concepts)
            score += max(0, repeats - 1) * CONCEPT_PRIORITY_STEP
        direct = normalize_search_text(query)
        if direct:
            values = {
                "jurabk": normalize_search_text(row["jurabk"]),
                "act_title": normalize_search_text(row["act_title"]),
                "enbez": normalize_search_text(row["enbez"]),
                "norm_title": normalize_search_text(row["norm_title"]),
                "text": normalize_search_text(row["body"]),
            }
            for field, value in values.items():
                if direct == value:
                    score += weights.get(field, 0) * 3
                elif direct in value:
                    score += weights.get(field, 0) * 2
        return score

    @staticmethod
    def _snippet(value: str, terms: list[str], fallback: str,
                 width: int = 260) -> str:
        """Return a compact official-spelling excerpt around the best term."""
        value = " ".join((value or fallback or "").split())
        if not value:
            return ""
        folded = normalize_search_text(value)
        positions = [(folded.find(term), term) for term in terms if term]
        positions = [(pos, term) for pos, term in positions if pos >= 0]
        if not positions:
            return value[:width] + (" …" if len(value) > width else "")
        pos, term = min(positions, key=lambda item: (item[0], -len(item[1])))
        # Folded and original offsets can differ around punctuation/diacritics;
        # proportional positioning is sufficient for choosing the excerpt,
        # while the returned text always remains the unmodified official text.
        ratio = len(value) / max(1, len(folded))
        centre = int((pos + len(term) / 2) * ratio)
        start = max(0, centre - width // 2)
        end = min(len(value), start + width)
        if end - start < width:
            start = max(0, end - width)
        excerpt = value[start:end].strip()
        return ("… " if start else "") + excerpt + \
            (" …" if end < len(value) else "")

    def _rows(self, expression: str, kind: str, limit: int,
              act_ids: set[str] | None = None) -> tuple[int, list]:
        act_filter = ""
        params: list[object] = [expression, kind]
        if act_ids:
            placeholders = ",".join("?" for _ in act_ids)
            act_filter = f" AND act_id IN ({placeholders})"
            params.extend(sorted(act_ids))
        total = self.conn.execute(
            "SELECT count(*) FROM search_fts WHERE search_fts MATCH ? "
            "AND kind = ?" + act_filter, params).fetchone()[0]
        rows = self.conn.execute(f"""
            SELECT rowid, *, bm25(search_fts,
                0.0, 0.0, 0.0, 0.0,
                14.0, 9.0, 12.0, 6.0, 1.0, 30.0,
                0.0, 0.0, 0.0, 0.0, 0.0) AS rank
            FROM search_fts
            WHERE search_fts MATCH ? AND kind = ?
            {act_filter}
            ORDER BY rank, act_id, enbez, rowid
            LIMIT ?
        """, [*params, limit]).fetchall()
        return total, rows

    def search(self, query: str, act_limit: int = 25,
               norm_limit: int = 50) -> dict:
        expression, terms, concepts = self._match_expression(query)
        norm_act_ids, skip_indexes = self._act_reference_constraint(query)
        norm_expression, _, _ = self._match_expression(
            query, norm=True, skip_token_indexes=skip_indexes)
        if not expression:
            return {"query": query, "total": 0, "matches": [],
                    "result_total": 0, "act_total": 0, "norm_total": 0,
                    "act_matches": [], "norm_matches": []}
        act_total, act_rows = self._rows(
            expression, "act", max(act_limit, len(self.wiki)))
        # Re-rank a generous FTS candidate window by field usefulness.  The
        # corpus is small (~11.6k norms), so this stays cheap while avoiding
        # short title-only rows dominating meaningful full-text hits.
        norm_candidates = min(3000, max(500, norm_limit * 10))
        norm_total, norm_rows = self._rows(
            norm_expression, "norm", norm_candidates, norm_act_ids)

        def rerank(rows: list[sqlite3.Row], kind: str) -> list[sqlite3.Row]:
            return sorted(rows, key=lambda row: (
                -self._relevance(
                    row, self._matched_fields(row, terms, kind, concepts),
                    query, kind, concepts),
                row["rank"], row["act_id"], row["enbez"], row["rowid"]))

        act_rows = rerank(act_rows, "act")[:act_limit]
        norm_rows = rerank(norm_rows, "norm")[:norm_limit]

        act_matches = []
        for position, row in enumerate(act_rows, 1):
            base = dict(self.wiki.get(row["act_id"]) or {
                "id": row["act_id"], "jurabk": row["jurabk"],
                "juris": row["juris"], "title": row["act_title"]})
            fields = self._matched_fields(row, terms, "act", concepts)
            display = row["act_title"] if "act_title" in fields \
                else row["jurabk"]
            base.update({
                "score": self._relevance(
                    row, fields, query, "act", concepts),
                "snippet": self._snippet(display, terms, display),
                "matched_fields": fields,
                "source": row["source"],
                "url": f"/acts/{row['act_id']}",
            })
            act_matches.append(base)

        norm_matches = []
        for position, row in enumerate(norm_rows, 1):
            fields = self._matched_fields(row, terms, "norm", concepts)
            snippet_source = row["body"] if "text" in fields else \
                row["norm_title"] or row["act_title"]
            norm_matches.append({
                "act_id": row["act_id"],
                "jurabk": row["jurabk"],
                "juris": row["juris"],
                "act_title": row["act_title"],
                "enbez": row["enbez"],
                "norm_title": row["norm_title"],
                "snippet": self._snippet(snippet_source, terms,
                                         row["act_title"]),
                "score": self._relevance(
                    row, fields, query, "norm", concepts),
                "matched_fields": fields,
                "source": row["source"],
                "url": f"/acts/{row['act_id']}",
            })

        # ``matches`` and ``total`` deliberately retain the pre-FTS contract:
        # a ranked list/count of acts in the exact wiki row shape.
        matches = [{key: value for key, value in row.items()
                    if key not in {"score", "snippet", "matched_fields",
                                   "source", "url"}}
                   for row in act_matches]
        return {
            "query": query,
            "total": act_total,
            "matches": matches,
            "result_total": act_total + norm_total,
            "act_total": act_total,
            "norm_total": norm_total,
            "act_matches": act_matches,
            "norm_matches": norm_matches,
        }
