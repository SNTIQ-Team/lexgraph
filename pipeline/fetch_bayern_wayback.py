"""Backfill historical word-diffs for BAVARIAN law from the Wayback Machine.

Bavaria publishes no historical consolidations, but web.archive.org has been
capturing the per-norm pages of gesetze-bayern.de (/Content/Document/<KEY>-<nr>)
since the 2015/16 relaunch. The BAYERN.RECHT version log tells us WHEN an act
changed and usually WHICH Artikel ("Art. 12 geänd."); for every such event we
locate the nearest capture BEFORE and AFTER the change date, extract the norm
text (div.absatz.paratext — markup stable across eras; pages even self-declare
"Text gilt ab: DD.MM.YYYY") and, when the texts differ, append an old/new pair
to the SAME cumulative ledger the daily snapshot-diff engine uses
(data/by_diffs.jsonl) — build_web_data.py merges it into the act versions
automatically.

Coverage is honest, not total: events before ~2016 have no captures, sparse
norms may lack a bracket, and "mehrfach geänd." events sweep every current
norm of the act. Politeness: CDX and page fetches are cached on disk (the run
is resumable), throttled, and back off hard when archive.org rate-limits.

One-shot:  python3 pipeline/fetch_bayern_wayback.py [--acts BayVwVfG,BayEUG]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from common import latest_snapshot, read_jsonl

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "by_diffs.jsonl"
CACHE = ROOT / "data" / "snapshots" / "wayback_by"
UA = "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)"

# ONE prefix query per act returns the capture index for EVERY norm page —
# ~10 CDX calls total instead of one per norm (~700).
CDX_ACT = ("http://web.archive.org/cdx/search/cdx?url="
           "gesetze-bayern.de/Content/Document/{key}-&matchType=prefix"
           "&output=json&fl=original,timestamp&filter=statuscode:200")
PAGE = ("https://web.archive.org/web/{ts}id_/"
        "https://www.gesetze-bayern.de/Content/Document/{doc}")

# archive coverage starts with the 2015/16 site relaunch
MIN_EVENT = "2016-01-01"

# Page fetches run on a small pool; each worker keeps its own politeness
# delay, so the aggregate stays ~3 req/s against web.archive.org.
WORKERS = 3


def http_get(url: str, delay: float, tries: int = 5) -> str | None:
    for attempt in range(tries):
        time.sleep(delay if attempt == 0 else 45 * attempt)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=90)
            if r.status_code == 200 and r.text.strip():
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
    return None


def cdx_act(key: str) -> dict[str, list[str]]:
    """{doc -> sorted capture timestamps} for all of an act's norm pages,
    from a single prefix CDX query (disk-cached)."""
    f = CACHE / "cdx-act" / f"{key}.json"
    if f.is_file():
        return json.loads(f.read_text())
    body = http_get(CDX_ACT.format(key=key), delay=3.0)
    out: dict[str, list[str]] = {}
    if body:
        try:
            rows = json.loads(body)[1:]
        except (ValueError, IndexError):
            rows = []
        for original, ts in rows:
            m = re.match(
                rf"https?://[^/]*gesetze-bayern\.de/Content/Document/"
                rf"({re.escape(key)}-\d+[a-z]?)$", original)
            if m:
                out.setdefault(m.group(1), []).append(ts)
        out = {d: sorted(set(t)) for d, t in out.items()}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


def fetch_norm(doc: str, ts: str) -> tuple[str | None, str] | None:
    """(gilt_ab_date, text) of one archived norm page (disk-cached)."""
    f = CACHE / "pages" / f"{doc}-{ts}.json"
    if f.is_file():
        d = json.loads(f.read_text())
        return d["valid"], d["text"]
    html = http_get(PAGE.format(ts=ts, doc=doc), delay=1.0)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("div#content") or soup
    chrome = re.sub(r"\s+", " ", content.get_text(" "))
    m = re.search(r"(?:Text gilt ab|in Kraft ab):\s*(\d{2})\.(\d{2})\.(\d{4})",
                  chrome)
    valid = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
    parts = [re.sub(r"\s+", " ", d.get_text(" ")).strip()
             for d in content.select("div.absatz.paratext")]
    text = "\n".join(p for p in parts if p)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"valid": valid, "text": text},
                            ensure_ascii=False))
    return valid, text


NR = r"\d+[a-z]?"


def affected_nrs(description: str, all_nrs: list[str]) -> tuple[list[str], bool]:
    """Norm numbers an event names; (all_nrs, False) when it names none
    ('mehrfach geänd.', structural edits). Second value: explicitly named."""
    d = description
    nrs: list[str] = []
    for m in re.finditer(rf"Art\.\s*({NR})\s*(?:[-–]\s*({NR}))?", d):
        a, b = m.group(1), m.group(2)
        if b and a in all_nrs and b in all_nrs:
            nrs.extend(all_nrs[all_nrs.index(a):all_nrs.index(b) + 1])
        elif b:
            nrs.extend([a, b])
        else:
            nrs.append(a)
        # trailing enumeration: "Art. 12, 16 geänd."
        tail = d[m.end():]
        for t in re.finditer(rf"^\s*,\s*({NR})(?=[\s,])", tail):
            nrs.append(t.group(1))
    nrs = [n for n in dict.fromkeys(nrs) if n in all_nrs]
    return (nrs, True) if nrs else (list(all_nrs), False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--acts", help="comma-separated jurabk filter")
    args = ap.parse_args()

    br = latest_snapshot("bayern_recht")
    if not br:
        print("run fetch_bayern_recht.py first", file=sys.stderr)
        return 1
    acts = {a["jurabk"]: a["key"] for a in read_jsonl(br / "acts.jsonl")}
    if args.acts:
        keep = {s.strip() for s in args.acts.split(",")}
        acts = {j: k for j, k in acts.items() if j in keep or k in keep}
    norms_by_act: dict[str, list[str]] = {}
    for n in read_jsonl(br / "norms.jsonl"):
        m = re.match(rf"(?:Art\.|§)\s*({NR})$", n.get("enbez") or "")
        if m:
            norms_by_act.setdefault(n["jurabk"], []).append(m.group(1))
    events = [v for v in read_jsonl(br / "versions.jsonl")
              if (v.get("date") or "") >= MIN_EVENT]

    existing = set()
    if LEDGER.is_file():
        existing = {(r["jurabk"], r["date"], r["para"])
                    for r in read_jsonl(LEDGER)}

    # process smaller acts first so results land early
    added = misses = 0
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    for jb, key in sorted(acts.items(),
                          key=lambda kv: len(norms_by_act.get(kv[0], []))):
        all_nrs = norms_by_act.get(jb) or []
        evs = sorted((e for e in events if e["jurabk"] == jb),
                     key=lambda e: e["date"])
        if not evs or not all_nrs:
            continue
        print(f"== {jb} ({key}): {len(evs)} events, {len(all_nrs)} norms")
        act_cdx = cdx_act(key)
        for ev in evs:
            date = ev["date"]
            d8 = date.replace("-", "")
            nrs, explicit = affected_nrs(ev.get("description") or "", all_nrs)
            # resolve each norm's capture bracket first, then fetch the whole
            # event's pages on the pool
            jobs: list[tuple[str, str, str | None, str]] = []
            for nr in nrs:
                para = f"Art. {nr}"
                if (jb, date, para) in existing:
                    continue
                doc = f"{key}-{nr}"
                ts_all = act_cdx.get(doc) or []
                before = [t for t in ts_all if t[:8] < d8]
                after = [t for t in ts_all if t[:8] >= d8]
                if not after or (not before and not explicit):
                    misses += 1
                    continue
                jobs.append((nr, after[0], before[-1] if before else None, doc))
            fetch_list = sorted({(doc, ts) for _, aft, bef, doc in jobs
                                 for ts in ([aft] + ([bef] if bef else []))})
            results = dict(zip(fetch_list,
                               pool.map(lambda j: fetch_norm(*j), fetch_list)))
            for nr, aft, bef, doc in jobs:
                para = f"Art. {nr}"
                new_cap = results.get((doc, aft))
                if new_cap is None:
                    misses += 1
                    continue
                if bef:
                    old_cap = results.get((doc, bef))
                    if old_cap is None:
                        misses += 1
                        continue
                    old_text = old_cap[1]
                else:
                    old_text = ""  # explicitly-named brand-new norm
                if old_text == new_cap[1] or not new_cap[1]:
                    continue  # this event did not touch this norm
                # 4000 (vs buzer's 1200): these are complete norms, not
                # excerpts — a clipped diff would cut mid-Absatz
                row = {"jurabk": jb, "date": date, "para": para,
                       "old": old_text[:4000], "new": new_cap[1][:4000]}
                with LEDGER.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                existing.add((jb, date, para))
                added += 1
                print(f"   + {date} {para}")
    pool.shutdown()
    print(f"\nledger +{added} diffs ({misses} unresolvable) -> {LEDGER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
