"""Fetch Bavarian promulgation LexEvents from verkuendung-bayern.de.

VERIFIED 2026-07-06 — two RSS 2.0 feeds, 50 items each, one item per
promulgated document:
  https://www.verkuendung-bayern.de/service/rss-feed/gvbl-rss/list/
  https://www.verkuendung-bayern.de/service/rss-feed/baymbl-rss/list/
Item shape: title; pubDate = Verkuendung date; description =
'"TITLE" vom DD.MM.YYYY, veroeffentlicht am DD.MM.YYYY
(Gliederungsnr. X[, Y])' (Gliederungsnr. may be empty, esp. BayMBl);
guid = stable permalink /gvbl/{YYYY}-{Nr}/ or /baymbl/{YYYY}-{Nr}/.
Detail pages (Fundstelle, PDF + SHA-256) exist but RSS alone carries
the event — we only spot-check one permalink per gazette.

Legal nuance encoded as `authenticity`: GVBl electronic PDFs are
"nachrichtlich" (the print edition is authoritative); the electronic
BayMBl IS the official promulgation ("amtlich"). Terms: § 5 UrhG
amtliche Werke; no robots.txt (302s into the search app); politeness
delay 1.0 s.

Output (data/snapshots/gvbl_events/<date>/): events.jsonl
    {event_id, kind, jurisdiction, gazette, time, ausfertigung_date,
     title, gliederungs_nr, permalink, authenticity, legal_effect,
     source, fetched_at}

Usage:
    python3 pipeline/fetch_gvbl_events.py [--no-spot-check]
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from common import Http, snapshot_dir, write_jsonl

FEEDS = {
    "GVBl": ("https://www.verkuendung-bayern.de/service/rss-feed/"
             "gvbl-rss/list/", "nachrichtlich"),
    "BayMBl": ("https://www.verkuendung-bayern.de/service/rss-feed/"
               "baymbl-rss/list/", "amtlich"),
}
GUID_RE = re.compile(r"/(gvbl|baymbl)/(\d{4})-(\d+)/?$")
# description = '"TITLE" vom D, veröffentlicht am D (…)'; anchor on the
# closing quote so a date inside TITLE can never win, fallback = first
VOM_ANCHORED_RE = re.compile(
    r'["“”]\s*vom (\d{2})\.(\d{2})\.(\d{4}),\s*ver')
VOM_RE = re.compile(r"vom (\d{2})\.(\d{2})\.(\d{4})")
GLIED_RE = re.compile(r"\(Gliederungsnr\.\s*([^)]*)\)")


def parse_items(xml_bytes: bytes, gazette: str, authenticity: str,
                fetched_at: str) -> tuple[list[dict], int]:
    """One LexEvent per RSS item; returns (events, failed_count)."""
    events, failed = [], 0
    for item in ET.fromstring(xml_bytes).iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        desc = " ".join((item.findtext("description") or "").split())
        guid = (item.findtext("guid") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        m = GUID_RE.search(guid)
        if not m:                       # no stable id -> honest failure
            failed += 1
            print(f"  [fail] {gazette}: unparseable guid {guid!r}")
            continue
        try:
            when = parsedate_to_datetime(pub).date().isoformat()
        except Exception:               # noqa: BLE001
            when = None
        vom = VOM_ANCHORED_RE.search(desc) or VOM_RE.search(desc)
        ausfertigung = (f"{vom.group(3)}-{vom.group(2)}-{vom.group(1)}"
                        if vom else None)
        gl = GLIED_RE.search(desc)
        glied = gl.group(1).strip() if gl and gl.group(1).strip() else None
        events.append({
            "event_id": f"event:{m.group(1)}:{m.group(2)}-{m.group(3)}"
                        ":published",
            "kind": "published",
            "jurisdiction": "DE-BY",
            "gazette": gazette,
            "time": when,
            "ausfertigung_date": ausfertigung,
            "title": title,
            "gliederungs_nr": glied,
            "permalink": guid,
            "authenticity": authenticity,
            "legal_effect": "publishes_law",
            "source": "verkuendung_bayern",
            "fetched_at": fetched_at,
        })
    return events, failed


def spot_check(http: Http, permalink: str) -> bool:
    """Non-fatal sanity check: permalink resolves to a document page.

    Both gazettes' detail pages carry the PDF hash marker "sha256";
    "Fundstelle" appears only on GVBl pages (verified 2026-07-06).
    """
    try:
        r = http.get(permalink, timeout=30)
        ok = r.status_code == 200 and "sha256" in r.text
    except Exception as exc:            # noqa: BLE001
        print(f"  [warn] spot-check error on {permalink}: {exc}")
        return False
    print(f"  [{'ok' if ok else 'warn'}] detail {permalink} "
          f"({r.status_code}, sha256 {'found' if ok else 'missing'})")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-spot-check", action="store_true",
                    help="skip the one detail-page check per gazette")
    args = ap.parse_args()

    http = Http(delay=1.0)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    events, failed = [], 0
    for gazette, (url, authenticity) in FEEDS.items():
        print(f"[{gazette}] fetching feed …")
        r = http.get(url, timeout=60)
        if r.status_code != 200:
            print(f"[{gazette}] HTTP {r.status_code} — skipping feed")
            failed += 1
            continue
        try:
            evs, bad = parse_items(r.content, gazette, authenticity,
                                   fetched_at)
        except ET.ParseError as exc:    # maintenance/error HTML page
            print(f"[{gazette}] feed unparseable ({exc}) — skipping")
            failed += 1
            continue
        print(f"[{gazette}] {len(evs)} events, {bad} failed items")
        if evs and not args.no_spot_check:
            spot_check(http, evs[0]["permalink"])
        events.extend(evs)
        failed += bad

    # guid is the identity; duplicates across fetches would be a bug
    if not events:
        print("[err] no events from any feed — snapshot kept as-is")
        return 1
    dupes = len(events) - len({e["event_id"] for e in events})
    out = snapshot_dir("gvbl_events")
    n = write_jsonl(out / "events.jsonl", events)
    print(f"\n{n} events ({failed} failed, {dupes} duplicate ids) -> {out}")
    for e in events[:2] + events[-2:]:
        print(f"  {e['gazette']:>6}  {e['time']}  {e['title'][:58]}")
    return 0 if events and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
