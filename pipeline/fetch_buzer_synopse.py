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
# The norm a change belongs to comes from the ROW STRUCTURE, not the change
# text: buzer inserts noprint navigation rows ("… Fassung von § 1a …",
# "aktuelle Fassung § 1a zeigen") and a heading row ("§ 1a Anspruchs-
# einschränkung") before each norm's rows. The old approach — regexing the
# change text itself — misattributed anything whose text merely CITES another
# norm first (e.g. the new § 1a Abs. 7 starts with "… nach § 1 Absatz 1 …"
# and was filed under § 1).
NAV_PARA = re.compile(
    r"(?:Fassung\s+von|aktuelle\s+Fassung)\s+(?:§|Art(?:ikel)?\.?)\s*(\d+\w*)", re.I)
HEAD_PARA = re.compile(r"^(?:§|Art(?:ikel)?\.?)\s*(\d+\w*)\b")


def cell_text(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ")).strip()


def parse_synopse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    changes = []
    current: str | None = None
    for tr in soup.find_all("tr"):
        row_text = re.sub(r"\s+", " ", tr.get_text(" ")).strip()
        halt, hneu = tr.find(class_="halt"), tr.find(class_="hneu")
        if not (halt and hneu):
            # navigation / heading rows update which norm we are inside
            m = NAV_PARA.search(row_text)
            if m:
                current = m.group(1)
            else:
                h = HEAD_PARA.match(row_text)
                # short row starting with '§ N <title>' = a norm heading;
                # body rows start with '(1) …', so they never match here
                if h and len(row_text) < 120:
                    current = h.group(1)
            continue
        old, new = cell_text(halt), cell_text(hneu)
        if old == new or not (old or new):
            continue
        if META.search(old) or META.search(new):
            old = META.sub("", old).strip()
            new = META.sub("", new).strip()
            if old == new or not (old or new):
                continue
        if current is None and HEAD_PARA.match(new or old):
            # table-of-contents diff rows precede the first norm marker; the
            # norm's own heading row repeats this information further down
            continue
        changes.append({"para": current, "old": old[:1200], "new": new[:1200]})
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
