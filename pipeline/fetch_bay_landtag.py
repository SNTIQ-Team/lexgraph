"""Fetch Bayerischer Landtag WP19 Gesetzentwuerfe with full lifecycle.

VERIFIED endpoints (2026-07-06):
- Facet search (server-rendered HTML, no JS):
  /parlament/dokumente/drucksachen?dokumentenart=Drucksache
      &ist_basisdokument=on&suchvorgangsarten[0]=Gesetze\\Gesetzentwurf
      &wahlperiodeid[0]=19&anzahl_treffer=50&sort=<date|nr>&page=N
  PITFALLS: without `dokumentenart=Drucksache` the server 302s and drops
  every facet; the vorgangsart value needs its category prefix
  ("Gesetze\\Gesetzentwurf", two literal backslashes) or the search
  returns "Keine Treffer". Date sort does not surface the newest items
  reliably, so we harvest BOTH sort orders to exhaustion and union by
  gegenstandid. Result row: h4>a = "Drucksache Nr. 19/NNNN vom
  DD.MM.YYYY" + PDF href, first p = initiators, h5>strong = title,
  vorgangsanzeige link carries gegenstandid.
- Per-bill lifecycle (JSF page, plain GET works):
  /webangebot3/views/vorgangsanzeige/vorgangsanzeige.xhtml?gegenstandid=N
  table#vorgangsanzeigedokumente: one tr per station; td[0] date or
  "Beratung / Ergebnis folgt" (pending), td[1] = station label, doc refs
  with PDF links, and a bare result line ("Ueberweisung", "Zustimmung
  [in geaenderter Fassung]", "Ablehnung", ...). Enacted bills end in a
  "Gesetz- und Verordnungsblatt Nr. N Seite X-Y" row with a mirrored
  GVBl excerpt PDF under /www/ElanTextAblage_WP19/GVBl/.
- RSS (titel param is MANDATORY, any non-empty value):
  /webangebot3/views/rssfeed/rssfeed.xhtml?art=GESETZ&titel=x
  /webangebot3/views/rssfeed/rssfeed.xhtml?art=BESCHL&titel=x

robots.txt (verified): Disallow /webangebot2/Vorgangsmappe, /*?eID=*,
/sdl, /service/suche — never touched here. vorgangsmappe.xhtml is
allowed but serves ~1.7 MB merged PDFs, so it is never fetched either;
RSS links pointing at it are only parsed for their gegenstandid.

Output (data/snapshots/bay_landtag/<date>/):
    bills.jsonl  {gegenstandid, drs_nr, titel, initiators[], eingang,
                  status, gvbl_citation?, pdf_url,
                  lifecycle:[{date, station, doc, doc_url?, organ?,
                              result?, excerpt_url?}], pending_stations}
    events.jsonl {event_id, kind, time, drs_nr, title,
                  source:"rss_gesetz"|"rss_beschl"}

Usage:
    python3 pipeline/fetch_bay_landtag.py [--limit N]
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup, NavigableString

from common import Http, snapshot_dir, write_jsonl

BASE = "https://www.bayern.landtag.de"
SEARCH_URL = BASE + "/parlament/dokumente/drucksachen"
VORGANG_URL = (BASE + "/webangebot3/views/vorgangsanzeige/"
               "vorgangsanzeige.xhtml")
RSS_URL = BASE + "/webangebot3/views/rssfeed/rssfeed.xhtml"

SEARCH_PARAMS = {
    "dokumentenart": "Drucksache",          # required, else 302 w/o facets
    "ist_basisdokument": "on",
    "suchvorgangsarten[0]": "Gesetze\\\\Gesetzentwurf",
    "wahlperiodeid[0]": "19",
    "anzahl_treffer": "50",
}

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
DRS_RE = re.compile(r"\b(\d+/\d+)\b")
NOISE_SEGS = {"", "Video zum TOP", "Redner", "Download PDF"}
WITHDRAWN_RE = re.compile(
    r"zur(ü|ue)ckgezogen|zur(ü|ue)ckziehung|r(ü|ue)cknahme|"
    r"zur(ü|ue)ckgenommen", re.I)


def iso(d: str) -> str | None:
    m = DATE_RE.search(d or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def new_http() -> Http:
    http = Http(delay=0.8)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")
    return http


# ---------------------------------------------------------------- facet
def parse_result_row(row) -> dict | None:
    h4a = row.select_one("h4 a")
    h5 = row.select_one("h5 strong")
    va = row.find("a", href=re.compile(r"vorgangsanzeige\.xhtml"))
    if not (h4a and va):
        return None
    m = re.search(r"Nr\.\s*(\d+/\d+)\s+vom\s+(\d{2}\.\d{2}\.\d{4})",
                  h4a.get_text(" ", strip=True))
    gid = re.search(r"gegenstandid=(\d+)", va["href"])
    if not (m and gid):
        return None
    p = row.find("p")
    init = p.get_text(" ", strip=True) if p else ""
    init = re.sub(r"^Gesetzentwurf\s*", "", init).strip()
    return {
        "gegenstandid": gid.group(1),
        "drs_nr": m.group(1),
        "titel": h5.get_text(" ", strip=True) if h5 else "",
        "initiators": [s.strip() for s in init.split(", ") if s.strip()],
        "eingang": iso(m.group(2)),
        "pdf_url": h4a.get("href", ""),
    }


def harvest_bills(http: Http) -> tuple[dict[str, dict], int]:
    """Union of both sort orders (date sort alone is unreliable)."""
    seen: dict[str, dict] = {}
    total = 0
    for sort in ("date", "nr"):
        for page in range(1, 41):                 # hard cap: 2000 hits
            params = dict(SEARCH_PARAMS, sort=sort, page=str(page))
            r = http.get(SEARCH_URL, params=params, timeout=60)
            soup = BeautifulSoup(r.text, "html.parser")
            m = re.search(r"Treffer\s+([\d.]+)\s*-\s*([\d.]+)\s+von"
                          r"\s+([\d.]+)", soup.get_text())
            if m:
                lo, hi, tot = (int(g.replace(".", "")) for g in m.groups())
                total = max(total, tot)
            else:           # counter drift: don't let (0,0,0) end paging
                print("  [warn] Treffer counter unparseable — falling "
                      "back to empty-page termination")
                lo = hi = tot = None
            page_rows = 0
            for row in soup.select("div.row.result"):
                b = parse_result_row(row)
                if b:
                    page_rows += 1
                    seen.setdefault(b["gegenstandid"], b)
            print(f"  [facet sort={sort}] page {page}: {page_rows} rows "
                  f"({len(seen)} unique)")
            if page_rows == 0 or (tot is not None and hi >= tot):
                break       # server clamps page past end
    return seen, total


# ------------------------------------------------------------ lifecycle
def td_segments(td) -> list[dict]:
    """Split a station cell into <br>-delimited {text, url} segments.

    Nested divs (Redner accordions) and scripts are dropped first; what
    remains is flat: text nodes, <br>, <a> (PDF or video), svg/img.
    """
    for junk in td.find_all(["div", "script"]):
        junk.decompose()
    segs, txt, url = [], [], None

    def flush():
        nonlocal txt, url
        t = re.sub(r"\s+", " ", "".join(txt)).strip(" - ")
        if t not in NOISE_SEGS or url:
            segs.append({"text": t, "url": url})
        txt, url = [], None

    for node in td.children:
        if isinstance(node, NavigableString):
            txt.append(str(node))
        elif node.name == "br":
            flush()
        elif node.name == "a":
            href = node.get("href", "")
            if ".pdf" in href.lower():
                url = url or href
            elif href and href != "#":       # e.g. Lobbyregister org name
                txt.append(" " + node.get_text(" ", strip=True))
    flush()
    return segs


def classify_row(date: str, segs: list[dict], has_first: bool) -> dict:
    """Map one dated station row to a typed lifecycle event.

    Lesung numbering: a Plenum/Plenarprotokoll row is the 1. Lesung if
    it ends in "Ueberweisung" or no completed 1. Lesung was seen yet
    (covers "Abgesetzt v.d. TO" / "Keine Entscheidung" sittings that
    get retried); everything after a referral is the 2. Lesung.
    """
    label = segs[0]["text"] if segs else ""
    docs = [s for s in segs[1:] if s["url"]]
    results = [s["text"] for s in segs[1:]
               if not s["url"] and s["text"] not in NOISE_SEGS]
    ev = {"date": date, "station": "other", "doc": label}
    if segs and segs[0]["url"]:
        ev["doc_url"] = segs[0]["url"]
    if results:
        ev["result"] = " | ".join(results)

    if label.startswith("Initiativdrucksache"):
        ev["station"] = "initiativdrucksache"
    elif label.startswith("Schriftliche Stellungnahmen"):
        ev["station"] = "stellungnahme"
        if docs:
            ev["doc"] = f"Stellungnahme {docs[0]['text']}"
            ev["doc_url"] = docs[0]["url"]
    elif label.startswith("Gesetz- und Verordnungsblatt"):
        ev["station"] = "verkuendung"
        for d in docs:                       # per-bill GVBl excerpt PDF
            if "auszug" in d["text"].lower():
                ev["excerpt_url"] = d["url"]
    elif label.startswith("Plenum"):
        beschl = next((d for d in docs
                       if d["text"].startswith("Beschluss des Plenums")),
                      None)
        proto = next((d for d in docs
                      if d["text"].startswith("Plenarprotokoll")), None)
        if beschl:
            ev["station"] = "schlussabstimmung"
            ev["doc"], ev["doc_url"] = beschl["text"], beschl["url"]
        elif proto:
            res = (ev.get("result") or "").lower()
            first = "überweisung" in res or not has_first
            ev["station"] = "1_lesung" if first else "2_lesung"
            ev["doc"], ev["doc_url"] = proto["text"], proto["url"]
            aus = next((d for d in docs
                        if d["text"].startswith("Protokollauszug")), None)
            if aus:
                ev["excerpt_url"] = aus["url"]
        else:                                # sitting without protocol doc
            ev["station"] = "plenum"
    elif "usschuss" in label:                # Ausschuss fuer ...
        ev["station"] = "ausschuss"
        ev["organ"] = label
        if docs:
            ev["doc"], ev["doc_url"] = docs[0]["text"], docs[0]["url"]
    elif WITHDRAWN_RE.search(label):
        ev["station"] = "zurueckgezogen"
    return ev


def derive_status(events: list[dict]) -> tuple[str, str | None]:
    blob = " | ".join(e.get("doc", "") + " " + e.get("result", "")
                      + " " + e["station"] for e in events)
    gvbl = next((e for e in events if e["station"] == "verkuendung"), None)
    if gvbl:
        return "verkuendet", gvbl["doc"]
    if WITHDRAWN_RE.search(blob):
        return "zurueckgezogen", None

    def last(st):
        return next((e for e in reversed(events) if e["station"] == st),
                    None)
    fin = last("schlussabstimmung")
    if fin:
        res = (fin.get("result") or "").lower()
        return ("abgelehnt" if "ablehnung" in res else "beschlossen"), None
    l2 = last("2_lesung")
    if l2:
        res = (l2.get("result") or "").lower()
        return ("abgelehnt" if "ablehnung" in res else "2_lesung"), None
    if last("ausschuss"):
        return "ausschuss", None
    if last("1_lesung"):
        return "1_lesung", None
    return "eingebracht", None


def fetch_lifecycle(http: Http, gid: str,
                    counters: Counter) -> tuple[list[dict], int]:
    r = http.get(VORGANG_URL, params={"gegenstandid": gid}, timeout=60)
    soup = BeautifulSoup(r.text, "html.parser")
    tbody = soup.find(id="vorgangsanzeigedokumente_data")
    events, pending, has_first = [], 0, False
    for tr in tbody.find_all("tr", recursive=False) if tbody else []:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        date = iso(tds[0].get_text(strip=True))
        if not date:                          # "Beratung / Ergebnis folgt"
            pending += 1
            continue
        ev = classify_row(date, td_segments(tds[1]), has_first)
        res = (ev.get("result") or "").lower()
        if (ev["station"] == "ausschuss"
                or (ev["station"] == "1_lesung" and "überweisung" in res)):
            has_first = True                  # bill was referred
        if ev["station"] == "other":
            counters[f"label_other::{ev['doc'][:60]}"] += 1
        if ev.get("result"):
            counters[f"result::{ev['result'][:60]}"] += 1
        events.append(ev)
    return events, pending


# ------------------------------------------------------------------ rss
def fetch_rss(http: Http, art: str, source: str) -> list[dict]:
    r = http.get(RSS_URL, params={"art": art, "titel": "x"}, timeout=60)
    events = []
    for item in ET.fromstring(r.content).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"\s+", " ", item.findtext("description") or "")
        try:
            when = parsedate_to_datetime(
                item.findtext("pubDate") or "").isoformat()
        except Exception:                    # noqa: BLE001
            when = None
        m = DRS_RE.search(title)
        drs = m.group(1) if m else None
        if title.startswith("Beschluss des Plenums"):
            kind = "beschluss_plenum"
        elif title.startswith("Initiativdrucksache"):
            kind = "initiativdrucksache"
        else:
            kind = re.sub(r"[^a-z0-9]+", "_",
                          (title[:m.start()] if m else title)
                          .lower().strip()) or "unknown"
        gid = re.search(r"gegenstandid=(\d+)", link)
        events.append({
            "event_id": f"event:bay_landtag:"
                        f"{gid.group(1) if gid else drs}:{kind}:{art}",
            "kind": kind,
            "time": when,
            "drs_nr": drs,
            "title": desc.strip() or title,
            "source": source,
        })
    return events


# ----------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int,
                    help="only fetch lifecycle for first N bills (debug)")
    args = ap.parse_args()

    http = new_http()
    print("[facet] harvesting WP19 Gesetzentwurf base documents ...")
    seen, total = harvest_bills(http)
    print(f"[facet] {len(seen)} unique bills (server reports {total})")
    if total and len(seen) != total:
        print(f"[warn] union ({len(seen)}) != server total ({total})")

    bills, failed = [], 0
    counters: Counter = Counter()
    todo = sorted(seen.values(), key=lambda b: b["gegenstandid"])
    if args.limit:
        todo = todo[:args.limit]
    for i, bill in enumerate(todo, 1):
        gid = bill["gegenstandid"]
        try:
            events, pending = fetch_lifecycle(http, gid, counters)
        except Exception as exc:             # noqa: BLE001
            failed += 1
            print(f"  [fail] {bill['drs_nr']} (gid {gid}): {exc}")
            continue
        status, gvbl = derive_status(events)
        bill["status"] = status
        if gvbl:
            bill["gvbl_citation"] = gvbl
        bill["lifecycle"] = events
        bill["pending_stations"] = pending
        bills.append(bill)
        if i % 20 == 0 or i == len(todo):
            print(f"  [vorgang] {i}/{len(todo)} bills "
                  f"(last: {bill['drs_nr']} -> {status})")

    print("[rss] fetching GESETZ + BESCHL feeds ...")
    rss = (fetch_rss(http, "GESETZ", "rss_gesetz")
           + fetch_rss(http, "BESCHL", "rss_beschl"))

    out = snapshot_dir("bay_landtag")
    n_b = write_jsonl(out / "bills.jsonl", bills)
    n_e = write_jsonl(out / "events.jsonl", rss)
    print(f"\n{n_b} bills, {n_e} rss events -> {out}")
    print(f"lifecycle fetches: {len(bills)} ok, {failed} failed, "
          f"{len(seen) - len(todo)} skipped (--limit)")

    print("\nstatus histogram:")
    for st, n in Counter(b["status"] for b in bills).most_common():
        print(f"  {st:>16}  {n}")
    oddities = {k: v for k, v in counters.items()
                if k.startswith("label_other::")}
    if oddities:
        print("\nunmapped station labels:")
        for k, v in sorted(oddities.items()):
            print(f"  {v:3}  {k.split('::', 1)[1]}")
    print("\nresult strings seen:")
    for k, v in counters.most_common():
        if k.startswith("result::"):
            print(f"  {v:3}  {k.split('::', 1)[1]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
