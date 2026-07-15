"""Gesetzentwurf-Vorgänge of all 16 Landtage via parlamentsspiegel.de
(the only cross-Länder index; joint documentation portal of the
Landesparlamente, HTML-only).

Same technically available, permission-gated path as
fetch_parlamentsspiegel.py: server-rendered HTML search with GET permalinks; the
first request sets a JSESSIONID cookie which Http's requests.Session
keeps. Canonical endpoint after the redirect:

    https://www.parlamentsspiegel.de/suche
        query     full text; empty = match all
        type      vorgang | dokNr
        size      max 50 (size=100 SILENTLY falls back to 10!)
        page      0-based
        detail    true -> Sachgebiet/Urheber/Fundstelle inline
        qyVTyp    Gesetz -> Gesetzentwurf Vorgänge (verified)
        qyZeitAb/qyZeitBis  TT.MM.JJJJ or symbolic (letzterMonat,
                            heute); explicit dates verified working
        qyHerk    optional Land filter; unused — we want all 16

Result blocks are <div class="ps-vorgang"> carrying a composite ID
like BAY_V165554_D127498 (Land code prefix) in the title anchor's
aria-controls, the Titel, per-document labels/dates and direct PDF
links to the ORIGIN Landtag servers. Hit total + last page come from
the counter div naming the page size ("… Treffer/Seite" — NOT the
global corpus stat) and the ps-SeiteEnde item (data-seite, 1-based).

Window: 6 months (today - ~183 days .. heute). If that yields more
than 2000 Vorgänge the harvest narrows itself to 3 months and says so.

Known source pitfall (verified 2026-07-06): Bremen (HB) lags badly —
its newest Gesetz Vorgang in the portal was dated 2024-09-24, so
HB=0 in a recent window is the portal's ingestion gap, not a bug
(qyHerk=HB confirms 1000+ older HB Vorgänge exist).

relevant=true flags likely asylum/social relevance from the Titel
(asyl|flüchtling|aufnahme|migration|integration|abschieb|
unterbringung|sozial|bürgergeld and ae/ue spellings).

Output (data/snapshots/laender_bills/<date>/):
    bills.jsonl  {event_id, jurisdiction, titel, dok_nr?, datum?,
                  doc_urls[], relevant, kind, source, fetched_at}

Usage:
    python3 pipeline/fetch_laender_bills.py
"""
from __future__ import annotations

import re
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from common import Http, snapshot_dir, write_jsonl

SEARCH_URL = "https://www.parlamentsspiegel.de/suche"
SIZE = 50                             # verified server max — more -> 10!
WINDOW_DAYS = 183                     # ~6 months
NARROW_DAYS = 91                      # fallback if window overflows
MAX_TOTAL = 2000                      # narrow above this many hits
# portal codes -> ISO 3166-2 suffixes; absent codes are already ISO
ISO_LAND = {"BAY": "BY", "BLN": "BE", "BRA": "BB", "HES": "HE",
            "MEVO": "MV", "NDS": "NI", "RPF": "RP", "SAL": "SL",
            "SAC": "SN", "SACA": "ST", "THUE": "TH"}
LAND_CODES = {"BW", "BAY", "BLN", "BRA", "HB", "HH", "HES", "MEVO",
              "NDS", "NW", "RPF", "SAL", "SAC", "SACA", "SH", "THUE"}

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
DOKNR_RE = re.compile(r"\b(\d+/\d+)\b")
RELEVANT_RE = re.compile(
    r"(?i)asyl|fluechtling|flüchtling|aufnahme|migration|integration"
    r"|abschieb|unterbringung|sozial|buergergeld|bürgergeld")


def fetch_page(http: Http, page: int, zeit_ab: str) -> BeautifulSoup:
    r = http.get(SEARCH_URL, params={
        "query": "", "type": "vorgang", "size": str(SIZE),
        "page": str(page), "detail": "true", "qyVTyp": "Gesetz",
        "qyZeitAb": zeit_ab, "qyZeitBis": "heute"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} on page {page}")
    return BeautifulSoup(r.text, "html.parser")


def parse_totals(soup: BeautifulSoup) -> tuple[int | None, int]:
    """(total hits, last page). The counter div is the one that also
    names the page size ("… Treffer/Seite"); pagination's ps-SeiteEnde
    item carries the 1-based last page in data-seite."""
    total = None
    for div in soup.select("div.d-print-block"):
        t = div.get_text(" ", strip=True)
        if "Treffer/Seite" not in t:
            continue
        m = re.search(r"([\d.]+)\s+Vorgänge", t)
        if m:
            total = int(m.group(1).replace(".", ""))
        break
    end = soup.select_one("li.ps-SeiteEnde a[data-seite]")
    last = int(end["data-seite"]) if end else 1
    return total, last


def parse_vorgang(block, fetched_at: str) -> dict | None:
    """One ps-vorgang -> bill row, or None if it has no composite id."""
    a = block.select_one("a.ps-details[aria-controls]")
    if a is None:
        return None
    event_id = a["aria-controls"].strip()
    if event_id.startswith("ps-detail-"):
        event_id = event_id[len("ps-detail-"):]
    if not event_id:
        return None
    land = event_id.split("_", 1)[0]
    tspan = a.select_one("span")
    titel = tspan.get_text(" ", strip=True) if tspan is not None else ""

    # first p.ps-dokument = the Vorgang's primary document; later ones
    # are Folge-Dokumente (may render without href, e.g. Überweisungen)
    dok_nr = datum = None
    doc_urls: list[str] = []
    for i, p in enumerate(block.select("p.ps-dokument")):
        link = p.select_one("a[href]")
        href = (link.get("href") or "").strip() if link is not None else ""
        if href.startswith("http") and href not in doc_urls:
            doc_urls.append(href)
        if i == 0:
            label_a = p.select_one("a")
            label = (label_a.get_text(" ", strip=True)
                     if label_a is not None else "")
            m = DOKNR_RE.search(label)
            dok_nr = m.group(1) if m else (label or None)
            m = DATE_RE.search(p.get_text(" ", strip=True))
            if m:
                datum = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    row = {"event_id": event_id,
           "jurisdiction": f"DE-{ISO_LAND.get(land, land)}",
           "titel": titel, "doc_urls": doc_urls,
           "relevant": bool(RELEVANT_RE.search(titel)),
           "kind": "gesetzentwurf", "source": "parlamentsspiegel",
           "fetched_at": fetched_at}
    if dok_nr:
        row["dok_nr"] = dok_nr
    if datum:
        row["datum"] = datum
    return row


def main() -> int:
    if os.environ.get("LEXGRAPH_ENABLE_PARLAMENTSSPIEGEL_BULK") != "1":
        print("broad Parlamentsspiegel extraction is quarantined: explicit "
              "reuse permission required", file=sys.stderr)
        return 2
    http = Http(delay=1.0)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    window = WINDOW_DAYS
    zeit_ab = (date.today() - timedelta(days=window)).strftime("%d.%m.%Y")
    try:
        soup = fetch_page(http, 0, zeit_ab)
    except Exception as exc:                # noqa: BLE001
        print(f"[err] page 0 failed ({exc}) — refusing to overwrite "
              "snapshot with an empty crawl")
        return 1
    total, last = parse_totals(soup)
    print(f"[search] {total} Gesetzentwurf-Vorgänge, {last} page(s) "
          f"(size={SIZE}, {zeit_ab}..heute)")
    if total and total > MAX_TOTAL:
        window = NARROW_DAYS
        zeit_ab = (date.today()
                   - timedelta(days=window)).strftime("%d.%m.%Y")
        print(f"[note] window overflows {MAX_TOTAL} hits — narrowing "
              f"to ~3 months ({zeit_ab}..heute)")
        try:
            soup = fetch_page(http, 0, zeit_ab)
        except Exception as exc:            # noqa: BLE001
            print(f"[err] page 0 failed ({exc}) — refusing to "
                  "overwrite snapshot with an empty crawl")
            return 1
        total, last = parse_totals(soup)
        print(f"[search] {total} Gesetzentwurf-Vorgänge, {last} "
              f"page(s) after narrowing")
    if total and total > SIZE and last == 1:
        print("[warn] no pagination though total > page size")

    bills: list[dict] = []
    seen: set[str] = set()
    dups = skipped = failed_pages = 0
    page = 0
    while page < last:
        if page > 0:
            try:
                soup = fetch_page(http, page, zeit_ab)
            except Exception as exc:        # noqa: BLE001 — keep going
                print(f"[page {page}] FAILED: {exc}")
                failed_pages += 1
                page += 1
                continue
        blocks = soup.select("div.ps-vorgang")
        new = 0
        for b in blocks:
            row = parse_vorgang(b, fetched_at)
            if row is None:
                skipped += 1
                continue
            if row["event_id"] in seen:     # order can shift mid-crawl
                dups += 1
                continue
            seen.add(row["event_id"])
            bills.append(row)
            new += 1
        print(f"[page {page}] {len(blocks)} blocks -> {new} new")
        page += 1

    if not bills and failed_pages:
        print("[err] empty crawl with failures — snapshot kept as-is")
        return 1
    out = snapshot_dir("laender_bills")
    n = write_jsonl(out / "bills.jsonl", bills)

    by_land = Counter(b["event_id"].split("_", 1)[0] for b in bills)
    print("\nper-Land counts:")
    for code in sorted(LAND_CODES | set(by_land)):
        mark = "" if code in LAND_CODES else "  [warn: unknown code]"
        print(f"  {code:>5}  {by_land.get(code, 0):4}{mark}")
    relevant = sum(1 for b in bills if b["relevant"])
    no_url = sum(1 for b in bills if not b["doc_urls"])
    print(f"\nfetched {n} bills (site reports {total}), "
          f"{relevant} flagged relevant, {dups} duplicates, "
          f"{skipped} unparseable blocks, {failed_pages} failed pages, "
          f"{no_url} bills without doc URL -> {out}")
    if total is not None and n + dups != total:
        print(f"[warn] coverage gap: parsed {n + dups} of {total}")
    if failed_pages:
        print(f"[warn] PARTIAL harvest: {failed_pages} page(s) failed "
              "— rerun to fill the gap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
