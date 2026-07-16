"""Read-only exact query facade for the built statutory citation index."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


SCHEMA_VERSION = "1"


class CitationStoreError(RuntimeError):
    pass


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def citation_norm_key(value: object) -> str:
    key = _key(value)
    key = re.sub(r"^(?:artikel|art)\.?\s*", "art. ", key)
    key = re.sub(r"^§\s*", "§ ", key)
    return key


DISPLAY_COLUMNS = (
    "id", "status", "unresolved_reason", "kind",
    "source_act", "source_jurabk", "source_norm", "source_excerpt",
    "source_snapshot", "date_basis", "citation_text",
    "matched_aliases_json", "target_act", "target_jurabk", "target_norm",
    "target_pinpoint", "occurrence_count", "machine_extracted",
    "current_state_only", "legal_interpretation",
)


def query_citations(path: Path, *, act: str | None, norm: str | None,
                    direction: str, kind: str | None,
                    limit: int, offset: int) -> dict:
    """Run an exact indexed query and return one stable ordinal page."""
    if direction not in {"in", "out"}:
        raise CitationStoreError("direction must be 'in' or 'out'")
    if kind not in {None, "self", "cross_act"}:
        raise CitationStoreError("unsupported citation kind")
    if limit < 1 or offset < 0:
        raise CitationStoreError("invalid citation pagination")
    if not path.is_file():
        raise CitationStoreError(f"citation index missing: {path.name}")

    clauses: list[str] = []
    parameters: list[object] = []
    if act:
        act_key = _key(act)
        prefix = "source" if direction == "out" else "target"
        clauses.append(
            f"({prefix}_act_key = ? OR {prefix}_jurabk_key = ?)")
        parameters.extend((act_key, act_key))
    if norm:
        norm_key = citation_norm_key(norm)
        if direction == "out":
            clauses.append("source_norm_key = ?")
            parameters.append(norm_key)
        else:
            clauses.append(
                "(target_norm_key = ? OR target_pinpoint_key = ?)")
            parameters.extend((norm_key, norm_key))
    if kind:
        clauses.append("kind = ?")
        parameters.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            version = conn.execute(
                "SELECT value FROM citation_meta WHERE key='schema_version'"
            ).fetchone()
            if not version or version[0] != SCHEMA_VERSION:
                raise CitationStoreError(
                    "unsupported Lexgraph citation index schema")
            matched = conn.execute(
                "SELECT COUNT(*) FROM citation" + where, parameters
            ).fetchone()[0]
            select = ",".join(DISPLAY_COLUMNS)
            rows = conn.execute(
                f"SELECT {select} FROM citation{where} "
                "ORDER BY ordinal LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
    except CitationStoreError:
        raise
    except sqlite3.Error as exc:
        raise CitationStoreError(f"citation index query failed: {exc}") \
            from exc

    citations = []
    for source in rows:
        row = dict(source)
        try:
            row["matched_aliases"] = json.loads(
                row.pop("matched_aliases_json"))
        except (TypeError, ValueError) as exc:
            raise CitationStoreError(
                "citation index contains invalid alias JSON") from exc
        row["machine_extracted"] = bool(row["machine_extracted"])
        row["current_state_only"] = bool(row["current_state_only"])
        citations.append(row)
    return {"matched": matched, "citations": citations}


__all__ = [
    "CitationStoreError",
    "citation_norm_key",
    "query_citations",
]
