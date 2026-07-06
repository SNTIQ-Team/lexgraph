"""Cross-Länder monitor: asylum/social parliamentary activity in all
16 Landtage via parlamentsspiegel.de (joint documentation portal of
the Landesparlamente).

parlamentsspiegel.de has NO robots.txt (302 to the app) and no API/
RSS/sitemap — the server-rendered HTML search with GET permalinks is
the sanctioned path (verified 2026-07-06). The first request sets a
JSESSIONID cookie; Http's requests.Session keeps it. Canonical search
endpoint after the redirect:

    https://www.parlamentsspiegel.de/suche
        query     full text; supports "||" OR syntax
        type      vorgang | dokNr
        size      max 50 (size=100 SILENTLY falls back to 10!)
        page      0-based
        detail    true -> Sachgebiet/Urheber/Fundstelle inline
        qyZeitAb/qyZeitBis  symbolic dates (letzterMonat, heute) are
                            more reliable than explicit dates
        qyHerk    optional Land filter; unused — we want all 16

Result blocks are <div class="ps-vorgang"> carrying a composite ID
like BAY_V165554_D127498 (Land code prefix) in the title anchor's
aria-controls, the Titel, per-document labels/dates and direct PDF
links to the ORIGIN Landtag servers. Hit total + last page come from
the "N Vorgänge … Treffer/Seite" counter and the ps-SeiteEnde
pagination item (data-seite, 1-based).

Output (data/snapshots/laender_monitor/<date>/):
    events.jsonl  {event_id, jurisdiction, titel, dok_nr?, datum?,
                   doc_urls[], kind, source, fetched_at}

Usage:
    python3 pipeline/fetch_parlamentsspiegel.py
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from common import Http, snapshot_dir, write_jsonl

SEARCH_URL = "https://www.parlamentsspiegel.de/suche"
QUERY = ("Asyl || Aufnahmegesetz || Flüchtling || "
         "Asylbewerberleistungsgesetz")
SIZE = 50                             # verified server max — more -> 10!
# portal codes -> ISO 3166-2 suffixes (gvbl_events etc. use DE-BY style)
ISO_LAND = {"BAY": "BY", "BLN": "BE", "BRA": "BB", "HES": "HE",
            "MEVO": "MV", "NDS": "NI", "RPF": "RP", "SAL": "SL",
            "SAC": "SN", "SACA": "ST", "THUE": "TH"}
LAND_CODES = {"BW", "BAY", "BLN", "BRA", "HB", "HH", "HES", "MEVO",
              "NDS", "NW", "RPF", "SAL", "SAC", "SACA", "SH", "THUE"}

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
DOKNR_RE = re.compile(r"\b(\d+/\d+)\b")


def fetch_page(http: Http, page: int) -> BeautifulSoup:
    r = http.get(SEARCH_URL, params={
        "query": QUERY, "type": "vorgang", "size": str(SIZE),
        "page": str(page), "detail": "true",
        "qyZeitAb": "letzterMonat", "qyZeitBis": "heute"}, timeout=60)
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
    """One ps-vorgang -> event row, or None if it has no composite id."""
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

    row = {"event_id": event_id, "jurisdiction": f"DE-{ISO_LAND.get(land, land)}",
           "titel": titel, "doc_urls": doc_urls,
           "kind": "landtag_activity", "source": "parlamentsspiegel",
           "fetched_at": fetched_at}
    if dok_nr:
        row["dok_nr"] = dok_nr
    if datum:
        row["datum"] = datum
    return row


def main() -> int:
    http = Http(delay=1.0)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    events: list[dict] = []
    seen: set[str] = set()
    total: int | None = None
    dups = skipped = failed_pages = 0
    page, last = 0, 1
    while page < last:
        try:
            soup = fetch_page(http, page)
        except Exception as exc:            # noqa: BLE001 — keep going
            print(f"[page {page}] FAILED: {exc}")
            failed_pages += 1
            if page == 0:       # no result count, no rows — a write now
                print("[err] page 0 failed — refusing to overwrite "
                      "snapshot with an empty crawl")
                return 1        # would truncate a good same-day snapshot
            page += 1
            continue
        if page == 0:
            total, last = parse_totals(soup)
            print(f"[search] {total} Vorgänge, {last} page(s) "
                  f"(size={SIZE}, letzterMonat..heute)")
            if total and total > SIZE and last == 1:
                print("[warn] no pagination though total > page size")
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
            events.append(row)
            new += 1
        print(f"[page {page}] {len(blocks)} blocks -> {new} new")
        page += 1

    if not events and failed_pages:
        print("[err] empty crawl with failures — snapshot kept as-is")
        return 1
    out = snapshot_dir("laender_monitor")
    n = write_jsonl(out / "events.jsonl", events)

    by_land = Counter(e["event_id"].split("_", 1)[0] for e in events)
    print("\nper-Land counts:")
    for code in sorted(LAND_CODES | set(by_land)):
        mark = "" if code in LAND_CODES else "  [warn: unknown code]"
        print(f"  {code:>5}  {by_land.get(code, 0):4}{mark}")
    no_url = sum(1 for e in events if not e["doc_urls"])
    print(f"\nfetched {n} events (site reports {total}), "
          f"{dups} duplicates, {skipped} unparseable blocks, "
          f"{failed_pages} failed pages, {no_url} events without "
          f"doc URL -> {out}")
    if total is not None and n + dups != total:
        print(f"[warn] coverage gap: parsed {n + dups} of {total}")
    return 1 if failed_pages else 0


if __name__ == "__main__":
    sys.exit(main())
