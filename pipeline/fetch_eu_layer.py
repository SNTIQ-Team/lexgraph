"""Fetch the EU genesis layer: instruments referenced by DIP procedures
plus the GEAS/asylum core, German transposition measures, and OJ events.

VERIFIED endpoints (2026-07-06):
  - CELLAR SPARQL, anonymous GET with format=application/sparql-results+json:
    https://publications.europa.eu/webapi/rdf/sparql
    PITFALL: CELEX URIs (…/resource/celex/<CELEX>) are NOT subjects in the
    store — they resolve only via  ?work owl:sameAs <celex-uri> ; querying
    them directly returns empty silently.
    PITFALL: cdm:resource_legal_implements_resource_legal is EMPTY; national
    implementing measures point at the directive via
    cdm:measure_national_implementing_implements_resource_legal and carry
    cdm:work_title (German act title) + cdm:resource_legal_id_local (national
    OJ citation).  Calibrated against 32019L1937 -> 17 DEU sector-7 works
    incl. 72019L1937DEU_202304032 "Hessisches Hinweisgebermeldestellengesetz".
  - EUR-Lex RSS (HTML legal-content paths are WAF-blocked, HTTP 202):
    https://eur-lex.europa.eu/EN/display-feed.rss?rssId=222
    ("Acts of the Official Journal L", CELEX id in each item title).
Licensing: Decision 2011/833/EU — free reuse.

Instrument list = regex over DIP vorgang titles
(Richtlinie|Verordnung) (EU|EG) yyyy/n -> CELEX 3yyyy[LR]nnnn, plus a fixed
GEAS/asylum core that is always included.

Output (data/snapshots/eu_layer/<date>/):
    instruments.jsonl    {celex, kind, year, number, title, pub_date?,
                          in_force?, dip_vorgang_ids[], in_geas_core}
    transpositions.jsonl {directive_celex, mne_celex, title, citation?}
    eu_events.jsonl      {kind:"published", jurisdiction:"EU", celex,
                          title, time}

Usage:
    python3 pipeline/fetch_eu_layer.py
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from email.utils import parsedate_to_datetime

from common import Http, latest_snapshot, read_jsonl, snapshot_dir, \
    write_jsonl

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
RSS_OJ_L = "https://eur-lex.europa.eu/EN/display-feed.rss?rssId=222"
UA = "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)"

PREFIXES = """\
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
"""
LANG_DEU = "http://publications.europa.eu/resource/authority/language/DEU"
LANG_ENG = "http://publications.europa.eu/resource/authority/language/ENG"
COUNTRY_DEU = "http://publications.europa.eu/resource/authority/country/DEU"

# GEAS/asylum core — always part of the instrument list
GEAS_CORE = {
    "32024L1346",  # Aufnahmerichtlinie
    "32024R1348",  # Asylverfahrens-VO
    "32024R1351",  # Asyl- und Migrationsmanagement-VO
    "32024R1356",  # Screening-VO
    "32024R1358",  # Eurodac-VO
    "32024R1350",  # Resettlement
    "32001L0055",  # Massenzustrom / temporary protection
    "32011L0095",  # Qualifikationsrichtlinie
    "32013L0032",  # Asylverfahrens-RL alt
    "32013L0033",  # Aufnahme-RL alt
    "32008L0115",  # Rueckfuehrungsrichtlinie
}

DIP_RE = re.compile(
    r"(Richtlinie|Verordnung)\s*\((EU|EG)\)\s*(\d{4})/(\d+)")
# pre-Lisbon styles: "Richtlinie 2001/55/EG", "Verordnung (EG) Nr. 343/2003"
DIP_RE_OLD_L = re.compile(r"Richtlinie\s+(\d{4})/(\d+)/(?:EG|EU|EWG)")
DIP_RE_OLD_R = re.compile(
    r"Verordnung\s*\((?:EG|EWG)\)\s*Nr\.\s*(\d+)/(\d{4})")


def celex_uri(celex: str) -> str:
    return f"http://publications.europa.eu/resource/celex/{celex}"


def sparql(http: Http, query: str) -> list[dict]:
    """Run a SELECT; return bindings as {var: plain-string} dicts."""
    r = http.get(SPARQL, params={
        "query": query, "format": "application/sparql-results+json"},
        timeout=120)
    r.raise_for_status()
    out = []
    for b in r.json()["results"]["bindings"]:
        out.append({k: v["value"] for k, v in b.items()})
    return out


def batches(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def dip_instruments() -> dict[str, set[str]]:
    """CELEX -> set of DIP vorgang ids, from the latest DIP snapshot."""
    snap = latest_snapshot("dip")
    hits: dict[str, set[str]] = defaultdict(set)
    if snap is None or not (snap / "vorgaenge.jsonl").exists():
        print("[warn] no DIP snapshot found — GEAS core only")
        return hits
    for row in read_jsonl(snap / "vorgaenge.jsonl"):
        titel = row.get("titel") or ""
        for m in DIP_RE.finditer(titel):
            kind = "L" if m.group(1) == "Richtlinie" else "R"
            celex = f"3{m.group(3)}{kind}{int(m.group(4)):04d}"
            hits[celex].add(row["id"])
        for m in DIP_RE_OLD_L.finditer(titel):
            hits[f"3{m.group(1)}L{int(m.group(2)):04d}"].add(row["id"])
        for m in DIP_RE_OLD_R.finditer(titel):
            hits[f"3{m.group(2)}R{int(m.group(1)):04d}"].add(row["id"])
    print(f"[dip] {len(hits)} instruments referenced in vorgang titles "
          f"({snap})")
    return hits


def fetch_metadata(http: Http, celexes: list[str]) -> dict[str, dict]:
    """CELEX -> {title, pub_date, in_force} from CELLAR (batched VALUES)."""
    meta: dict[str, dict] = {}
    for batch in batches(celexes, 15):
        values = " ".join(f"<{celex_uri(c)}>" for c in batch)
        q = PREFIXES + f"""\
SELECT DISTINCT ?u ?lang ?title ?pub ?force ?inforce WHERE {{
  VALUES ?u {{ {values} }}
  ?w owl:sameAs ?u .
  OPTIONAL {{
    ?e cdm:expression_belongs_to_work ?w ;
       cdm:expression_uses_language ?lang ;
       cdm:expression_title ?title .
    FILTER(?lang IN (<{LANG_DEU}>, <{LANG_ENG}>))
  }}
  OPTIONAL {{ ?w cdm:work_date_document ?pub }}
  OPTIONAL {{ ?w cdm:resource_legal_date_entry-into-force ?force }}
  OPTIONAL {{ ?w cdm:resource_legal_in-force ?inforce }}
}}"""
        for row in sparql(http, q):
            celex = row["u"].rsplit("/", 1)[-1]
            m = meta.setdefault(celex, {})
            lang = row.get("lang", "")
            if "title" in row:
                key = "title_de" if lang == LANG_DEU else "title_en"
                m.setdefault(key, row["title"])
            if row.get("pub"):
                m.setdefault("pub_date", row["pub"])
            if row.get("force"):
                if row["force"] >= "1900":   # 1001-01-01 = CELLAR n/a
                    m.setdefault("in_force_date", row["force"])
            if row.get("inforce"):
                m.setdefault("in_force", row["inforce"] == "1")
    return meta


def fetch_mnes(http: Http, directives: list[str]) -> list[dict]:
    """German national implementing measures per directive (batched)."""
    rows, seen = [], set()
    for batch in batches(directives, 10):
        values = " ".join(f"<{celex_uri(c)}>" for c in batch)
        q = PREFIXES + f"""\
SELECT DISTINCT ?u ?mcx ?title ?cit ?ojn ?ojp ?ojd WHERE {{
  VALUES ?u {{ {values} }}
  ?dir owl:sameAs ?u .
  ?mne cdm:measure_national_implementing_implements_resource_legal ?dir ;
       cdm:measure_national_implementing_implemented_by_country
           <{COUNTRY_DEU}> ;
       cdm:resource_legal_id_celex ?mcx .
  OPTIONAL {{ ?mne cdm:work_title ?title }}
  OPTIONAL {{ ?mne cdm:resource_legal_id_local ?cit }}
  OPTIONAL {{ ?mne
      cdm:measure_national_implementing_number_official_journal ?ojn }}
  OPTIONAL {{ ?mne
      cdm:measure_national_implementing_page_official_journal ?ojp }}
  OPTIONAL {{ ?mne
      cdm:measure_national_implementing_date_official_journal ?ojd }}
}}"""
        for row in sparql(http, q):
            directive = row["u"].rsplit("/", 1)[-1]
            key = (directive, row["mcx"])
            if key in seen:
                continue
            seen.add(key)
            # citation: national OJ ref if given, else compose from parts
            cit = row.get("cit", "")
            if not cit and row.get("ojd"):
                cit = "nat. OJ " + ", ".join(filter(None, (
                    f"Nr. {row['ojn']}" if row.get("ojn") else "",
                    row["ojd"],
                    f"S. {row['ojp']}" if row.get("ojp") else "")))
            rec = {"directive_celex": directive, "mne_celex": row["mcx"],
                   "title": row.get("title", "")}
            if cit:
                rec["citation"] = cit
            rows.append(rec)
    return rows


def fetch_oj_events(http: Http) -> list[dict]:
    """Latest OJ L acts from the working EUR-Lex RSS feed."""
    r = http.get(RSS_OJ_L, timeout=60)
    r.raise_for_status()
    events = []
    for item in ET.fromstring(r.content).iter("item"):
        title = (item.findtext("title") or "").strip()
        # corrigenda (…R(nn)) come without a ": <title>" part — keep them
        m = re.match(r"CELEX:([0-9A-Za-z()_]+)(?::\s*(.*))?$", title, re.S)
        if not m:
            print(f"[warn] unparsed RSS item: {title[:80]}")
            continue
        when = ""
        pd = (item.findtext("pubDate") or "").strip()
        if pd:
            when = parsedate_to_datetime(pd).isoformat()
        events.append({"kind": "published", "jurisdiction": "EU",
                       "celex": m.group(1),
                       "title": " ".join((m.group(2) or "").split()),
                       "time": when})
    return events


def main() -> int:
    http = Http(delay=1.0)                      # politeness: 1 req/s SPARQL
    http.s.headers["User-Agent"] = UA

    dip_hits = dip_instruments()
    celexes = sorted(set(dip_hits) | GEAS_CORE)
    print(f"[list] {len(celexes)} instruments "
          f"({len(GEAS_CORE)} GEAS core, {len(dip_hits)} from DIP)")

    print("[cellar] fetching work metadata …")
    meta = fetch_metadata(http, celexes)
    resolved = sorted(set(meta))
    unresolved = sorted(set(celexes) - set(meta))
    if unresolved:
        print(f"[warn] not resolved in CELLAR: {unresolved}")

    instruments = []
    for c in celexes:
        m = meta.get(c, {})
        instruments.append({
            "celex": c,
            "kind": "directive" if c[5] == "L" else "regulation",
            "year": int(c[1:5]),
            "number": int(c[6:]),
            "title": m.get("title_de") or m.get("title_en") or "",
            "pub_date": m.get("pub_date"),
            "in_force": m.get("in_force"),
            "in_force_date": m.get("in_force_date"),
            "dip_vorgang_ids": sorted(dip_hits.get(c, ())),
            "in_geas_core": c in GEAS_CORE,
        })

    directives = [c for c in celexes if c[5] == "L"]
    print(f"[cellar] fetching DEU implementing measures for "
          f"{len(directives)} directives …")
    transpositions = fetch_mnes(http, directives)
    covered = {t["directive_celex"] for t in transpositions}
    print(f"[cellar] {len(transpositions)} MNEs across {len(covered)} "
          f"directives; none for: {sorted(set(directives) - covered)}")

    print("[rss] fetching OJ L feed …")
    events = fetch_oj_events(http)

    out = snapshot_dir("eu_layer")
    n_i = write_jsonl(out / "instruments.jsonl", instruments)
    n_t = write_jsonl(out / "transpositions.jsonl", transpositions)
    n_e = write_jsonl(out / "eu_events.jsonl", events)
    print(f"\ninstruments: {n_i} ({len(resolved)} resolved in CELLAR, "
          f"{len(unresolved)} unresolved)")
    print(f"transpositions: {n_t} MNEs for {len(covered)}/"
          f"{len(directives)} directives")
    print(f"eu_events: {n_e} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
