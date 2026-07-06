"""Fetch per-§ old/new text from buzer.de synopse pages.

The buzer version log gives us WHEN and WHICH §§ changed; the synopse page
(`/gesetz/<id>/v<n>-<date>.htm`) additionally carries the actual text —
two columns `halt` (alte Fassung) / `hneu` (neue Fassung) per norm, with
`hdiff` spans already marking the delta. We extract the changed pairs so
the visualizer can render a real local word-diff for historical
amendments instead of linking out, and so per-§ change granularity (incl.
future-dated versions = gestaffeltes Inkrafttreten) becomes queryable.

Scope: the practice corpus, recent versions only (fetching every synopse
since 2006 would be thousands of pages). Non-authoritative, like all buzer
data. Polite crawl (0.9 s), robots-respected (only version pages, never
/s2.htm search).

Output (data/snapshots/buzer_synopse/<date>/synopse.jsonl):
    {jurabk, act_id, date, url, changes:[{para, old, new}]}
"""
from __future__ import annotations

import argparse
import re
import sys
from http.cookiejar import DefaultCookiePolicy

from bs4 import BeautifulSoup

from common import Http, latest_snapshot, read_jsonl, snapshot_dir, \
    write_jsonl

BASE = "https://www.buzer.de"
# ignore the table-of-contents / meta rows and the column headers
META = re.compile(r"^\(Text (alte|neue) Fassung\)|Inhaltsübersicht", re.I)
PARA = re.compile(r"§\s*(\d+[a-z]?)|Art(?:ikel)?\.?\s*(\d+[a-z]?)")


def cell_text(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ")).strip()


def parse_synopse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    changes = []
    for tr in soup.find_all("tr"):
        halt, hneu = tr.find(class_="halt"), tr.find(class_="hneu")
        if not (halt and hneu):
            continue
        old, new = cell_text(halt), cell_text(hneu)
        if old == new or not (old or new):
            continue
        if META.search(old) or META.search(new):
            old = META.sub("", old).strip()
            new = META.sub("", new).strip()
            if old == new or not (old or new):
                continue
        m = PARA.search(new) or PARA.search(old)
        para = (m.group(1) or m.group(2)) if m else None
        changes.append({"para": para, "old": old[:1200], "new": new[:1200]})
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-01-01",
                    help="only versions on/after this date")
    ap.add_argument("--per-act", type=int, default=12,
                    help="cap synopse pages per act (newest first)")
    args = ap.parse_args()

    bz = latest_snapshot("buzer")
    if not bz:
        print("run fetch_buzer.py first", file=sys.stderr)
        return 1
    versions = [v for v in read_jsonl(bz / "versions.jsonl")
                if v.get("synopsis_url") and (v.get("date") or "") >= args.since]
    by_act: dict[str, list] = {}
    for v in sorted(versions, key=lambda x: x["date"], reverse=True):
        by_act.setdefault(v["jurabk"], []).append(v)

    http = Http(delay=0.9)
    http.s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    http.s.headers["User-Agent"] = \
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)"

    rows, n_pages = [], 0
    for jurabk, vs in by_act.items():
        for v in vs[:args.per_act]:
            r = http.get(v["synopsis_url"], timeout=45)
            n_pages += 1
            if r.status_code != 200:
                continue
            changes = parse_synopse(r.text)
            rows.append({"jurabk": jurabk, "act_id": v["act_id"],
                         "date": v["date"], "url": v["synopsis_url"],
                         "changes": changes})
        print(f"  {jurabk:>16}  {min(len(vs), args.per_act):2} pages, "
              f"{sum(len(r['changes']) for r in rows if r['jurabk']==jurabk):4} "
              f"§-diffs")

    out = snapshot_dir("buzer_synopse")
    write_jsonl(out / "synopse.jsonl", rows)
    total = sum(len(r["changes"]) for r in rows)
    print(f"\n{len(rows)} synopses, {total} §-level diffs, "
          f"{n_pages} pages fetched -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
