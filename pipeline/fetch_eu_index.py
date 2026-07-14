"""Full index of in-force EU directives and (basic) regulations from CELLAR.

The curated Lexgraph corpus tracks ~47 instruments in depth; this index adds
BREADTH: every directive in force (incl. delegated/implementing, ~1.3k) and
every basic Council/EP regulation in force (~6.6k) as metadata rows — CELEX,
type, document date, German title (English fallback). No texts: EUR-Lex is
one click away via CELEX. Implementing/delegated REGULATIONS (~9k rows of
technical machinery) are deliberately excluded.

Source: the Publications Office SPARQL endpoint (CELLAR). Paged politely.

Output (data/snapshots/eu_index/<date>/instruments.jsonl):
    {celex, type: "DIR"|"DIR_DEL"|"DIR_IMPL"|"REG", date, title}
"""
from __future__ import annotations

import os
import re
import sys
import time

import requests

from common import snapshot_dir, write_jsonl

ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
UA = "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)"
PAGE = 1000

TYPES = ["DIR", "DIR_DEL", "DIR_IMPL", "REG"]

# A broken/changed CELLAR query must never replace a good same-day snapshot
# with a plausible-looking partial index. These floors are deliberately well
# below the July 2026 valid-base counts (1,113 / 126 / 70 / 6,625), while
# still catching truncated pagination or an ontology regression.
MIN_COUNTS = {"DIR": 800, "DIR_DEL": 50, "DIR_IMPL": 25, "REG": 5000}

QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?celex ?date (SAMPLE(?tDe) AS ?titleDe) (SAMPLE(?tEn) AS ?titleEn)
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  ?work cdm:work_has_resource-type
        <http://publications.europa.eu/resource/authority/resource-type/{rtype}> .
  ?work cdm:resource_legal_in-force
        "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .
  ?work cdm:work_date_document ?date .
  OPTIONAL {{
    ?eDe cdm:expression_belongs_to_work ?work ;
         cdm:expression_uses_language
           <http://publications.europa.eu/resource/authority/language/DEU> ;
         cdm:expression_title ?tDe .
  }}
  OPTIONAL {{
    ?eEn cdm:expression_belongs_to_work ?work ;
         cdm:expression_uses_language
           <http://publications.europa.eu/resource/authority/language/ENG> ;
         cdm:expression_title ?tEn .
  }}
}}
GROUP BY ?celex ?date
ORDER BY DESC(?date) ?celex
LIMIT {limit} OFFSET {offset}
"""


def fetch_type(rtype: str) -> list[dict]:
    rows, offset = [], 0
    while True:
        q = QUERY.format(rtype=rtype, limit=PAGE, offset=offset)
        for attempt in range(4):
            try:
                r = requests.get(
                    ENDPOINT,
                    params={"query": q,
                            "format": "application/sparql-results+json"},
                    headers={"User-Agent": UA}, timeout=180)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError):
                time.sleep(20 * (attempt + 1))
        else:
            raise RuntimeError(
                f"CELLAR failed for {rtype} at offset {offset}; "
                "refusing to write a partial index")
        page = data["results"]["bindings"]
        for b in page:
            celex = b["celex"]["value"]
            # A handful of corrigenda/addenda inherit the parent REG/DIR
            # resource type in CELLAR (for example `32011R1178R(06)`), and
            # some sector X/Y works are mistyped REG. Parenthesized numeric
            # suffixes on the base identifier itself remain valid for a few
            # older Euratom/EEC acts (`31958R0001(01)`).
            letter = "L" if rtype.startswith("DIR") else "R"
            if not re.fullmatch(
                    rf"3\d{{4}}{letter}\d{{4}}(?:\(\d{{2}}\))?", celex):
                continue
            title = (b.get("titleDe") or b.get("titleEn") or {}).get("value")
            # Keep the instrument enumerable even if one CELLAR expression
            # temporarily lacks a DE/EN title.
            title = title or celex
            rows.append({"celex": celex, "type": rtype,
                         "date": b["date"]["value"][:10],
                         "title": " ".join(title.split())[:300]})
        print(f"  {rtype}: +{len(page)} (total {len(rows)})")
        if len(page) < PAGE:
            if len(rows) < MIN_COUNTS[rtype]:
                raise RuntimeError(
                    f"CELLAR returned only {len(rows)} {rtype} rows "
                    f"(minimum {MIN_COUNTS[rtype]}); refusing snapshot")
            if len({r["celex"] for r in rows}) != len(rows):
                raise RuntimeError(f"duplicate CELEX within {rtype} results")
            return rows
        offset += PAGE
        time.sleep(2)


def main() -> int:
    all_rows: dict[str, dict] = {}
    for rtype in TYPES:
        for row in fetch_type(rtype):
            # a work can carry several type triples — directives win over REG
            prev = all_rows.get(row["celex"])
            if prev is None or (prev["type"] == "REG" and rtype != "REG"):
                all_rows[row["celex"]] = row
    rows = sorted(all_rows.values(),
                  key=lambda r: (r["date"], r["celex"]), reverse=True)
    out = snapshot_dir("eu_index")
    target = out / "instruments.jsonl"
    temporary = out / "instruments.jsonl.tmp"
    write_jsonl(temporary, rows)
    os.replace(temporary, target)
    print(f"\n{len(rows)} in-force instruments -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
