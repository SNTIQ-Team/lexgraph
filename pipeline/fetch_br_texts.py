"""Fetch substantive Bundesrat Drucksachen texts behind thin DIP covers.

For BR-initiated bills DIP only carries a 1-2 page transmittal cover
letter; the real bill text is a bundesrat.de PDF. This fetcher scans
short DIP cache texts for referenced BR numbers ("NNN/YY") and pulls
the underlying Drucksache PDFs.

VERIFIED URL pattern (2026-07-06; five test numbers reproduced):
    https://www.bundesrat.de/SharedDocs/drucksachen/{YYYY}/
        {lo:04d}-{hi:04d}/{nr}-{yy}.pdf?__blob=publicationFile&v=1
    YYYY = 2000+yy, lo = ((nr-1)//100)*100+1, hi = lo+99
    (296/25 -> 2025/0201-0300/296-25.pdf). Beschluss variant: literal
    "(B)" before .pdf, e.g. 173-24(B).pdf (unencoded parens work).

VERIFIED pitfalls (2026-07-06):
  - robots.txt Crawl-delay: 30 is BINDING -> Http(delay=30.0), one
    progress line per request; full run takes ~15-30 minutes.
  - HEAD -> 303 to /error_path/400.html (Airlock WAF): GET only.
  - Same URL without ?__blob=publicationFile returns 200 text/html
    (detail page): require Content-Type application/pdf before saving.

Output:
    data/cache/br_text/{nr}-{yy}{""|"B"}.txt   extracted text; a .json
        sidecar holds the index row so cached numbers cost no requests
    data/snapshots/br_texts/<date>/index.jsonl
        {br_nr, variant, url, http_status, content_type, pdf_bytes,
         text_chars, has_artikel, kept, cached_path}

Usage:
    python3 pipeline/fetch_br_texts.py [--limit N]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys

from common import ROOT, Http, snapshot_dir, write_jsonl

DIP_CACHE = ROOT / "data" / "cache" / "dip_text"
BR_CACHE = ROOT / "data" / "cache" / "br_text"
COVER_MAX = 4000        # DIP texts below this are cover-letter suspects
SUBSTANTIVE_MIN = 3000  # BR texts below this trigger the (B) fallback
MAX_REQUESTS = 60       # hard politeness budget for one run


def br_url(nr: int, yy: int, beschluss: bool = False) -> str:
    lo = ((nr - 1) // 100) * 100 + 1
    b = "(B)" if beschluss else ""
    return (f"https://www.bundesrat.de/SharedDocs/drucksachen/{2000 + yy}/"
            f"{lo:04d}-{lo + 99:04d}/{nr}-{yy}{b}.pdf"
            f"?__blob=publicationFile&v=1")


def collect_targets() -> list[tuple[int, int]]:
    """BR numbers cited in short (cover-letter sized) DIP cache texts."""
    pat = re.compile(r"Drucksache\s+(\d+)/(\d+)")
    found: set[tuple[int, int]] = set()
    for p in sorted(DIP_CACHE.glob("*.txt")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) >= COVER_MAX:
            continue
        for m in pat.finditer(text):
            nr, yy = int(m.group(1)), int(m.group(2))
            if 20 <= yy <= 26:          # year-suffixed BR numbering
                found.add((nr, yy))
    # newest first: the actual WP21 gap gets the request budget before
    # older rounds that cover letters merely reference
    return sorted(found, key=lambda t: (-t[1], -t[0]))


def pdf_to_text(data: bytes) -> str:
    try:
        r = subprocess.run(["pdftotext", "-layout", "-", "-"],
                           input=data, capture_output=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.decode("utf-8", errors="replace")
    except Exception:                   # noqa: BLE001 — fall through
        pass
    try:                                # fallback: pypdf
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(data)).pages
        return "\n".join((pg.extract_text() or "") for pg in pages)
    except Exception:                   # noqa: BLE001
        return ""


def substantive(text: str) -> bool:
    return "Artikel 1" in text or "wird wie folgt geändert" in text


def fetch_variant(http: Http, nr: int, yy: int,
                  beschluss: bool) -> tuple[dict, str]:
    url = br_url(nr, yy, beschluss)
    try:
        r = http.get(url, timeout=120)
        status = r.status_code
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
        body = r.content
    except Exception as exc:            # noqa: BLE001 — record, move on
        status, ctype, body = 0, f"error:{type(exc).__name__}", b""
    row = {"br_nr": f"{nr}/{yy}",
           "variant": "beschluss" if beschluss else "base",
           "url": url, "http_status": status, "content_type": ctype,
           "pdf_bytes": 0, "text_chars": 0, "has_artikel": False,
           "kept": False, "cached_path": None}
    text = ""
    if status == 200 and ctype == "application/pdf":
        row["pdf_bytes"] = len(body)
        text = pdf_to_text(body)
        row["text_chars"] = len(text)
        row["has_artikel"] = substantive(text)
    return row, text


def load_cached(nr: int, yy: int) -> dict[str, dict]:
    """Per-VARIANT cache: a cached base must not freeze out a still-
    missing (B) fetch, and vice versa."""
    out = {}
    for suffix, variant in (("", "base"), ("B", "beschluss")):
        meta = BR_CACHE / f"{nr}-{yy}{suffix}.json"
        if meta.is_file():
            out[variant] = json.loads(meta.read_text(encoding="utf-8"))
    return out


def save(row: dict, text: str) -> None:
    suffix = "B" if row["variant"] == "beschluss" else ""
    nr, yy = row["br_nr"].split("/")
    txt = BR_CACHE / f"{nr}-{yy}{suffix}.txt"
    txt.write_text(text, encoding="utf-8")
    row["cached_path"] = str(txt.relative_to(ROOT))
    (BR_CACHE / f"{nr}-{yy}{suffix}.json").write_text(
        json.dumps(row, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="process only first N numbers")
    args = ap.parse_args()

    BR_CACHE.mkdir(parents=True, exist_ok=True)
    targets = collect_targets()
    print(f"[scan] {len(targets)} candidate BR numbers in "
          f"{DIP_CACHE.relative_to(ROOT)}")
    if args.limit:
        targets = targets[: args.limit]

    # 30 s crawl delay is BINDING (robots.txt); retries=1 so any manual
    # re-attempt goes through .get() again and waits the full delay.
    http = Http(delay=30.0, retries=1)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")

    rows: list[dict] = []
    reqs = cache_hits = pdf_ok = failed = over_budget = 0
    kept_base = kept_beschluss = no_text = 0

    for i, (nr, yy) in enumerate(targets, 1):
        cache = load_cached(nr, yy)
        rows.extend(cache.values())
        fetched: list[tuple[dict, str]] = []
        row = cache.get("base")
        if row is None:
            if reqs >= MAX_REQUESTS:
                over_budget += 1
                print(f"[{i:2}/{len(targets)}] {nr}/{yy}  SKIPPED "
                      f"(request budget {MAX_REQUESTS} exhausted)")
                continue
            row, text = fetch_variant(http, nr, yy, beschluss=False)
            reqs += 1
            fetched.append((row, text))
            print(f"[{i:2}/{len(targets)}] {nr}/{yy}  base      -> "
                  f"{row['http_status']} {row['content_type'] or '-':24} "
                  f"{row['pdf_bytes']:>8}B {row['text_chars']:>7}ch"
                  f"{'  ART' if row['has_artikel'] else ''}", flush=True)

        need_b = (row["text_chars"] < SUBSTANTIVE_MIN
                  or not row["has_artikel"])
        if not fetched and (not need_b or "beschluss" in cache):
            cache_hits += 1
            print(f"[{i:2}/{len(targets)}] {nr}/{yy}  cached "
                  f"({len(cache)} variant(s))")
            continue
        if need_b and "beschluss" not in cache and reqs < MAX_REQUESTS:
            rowb, textb = fetch_variant(http, nr, yy, beschluss=True)
            reqs += 1
            fetched.append((rowb, textb))
            print(f"[{i:2}/{len(targets)}] {nr}/{yy}  beschluss -> "
                  f"{rowb['http_status']} "
                  f"{rowb['content_type'] or '-':24} "
                  f"{rowb['pdf_bytes']:>8}B {rowb['text_chars']:>7}ch"
                  f"{'  ART' if rowb['has_artikel'] else ''}", flush=True)

        # keep the variant with substantive text; tie-break on length
        winner = max(fetched,
                     key=lambda ft: (ft[0]["has_artikel"],
                                     ft[0]["text_chars"]))
        for r, t in fetched:
            if r["text_chars"] > 0:
                r["kept"] = r is winner[0]
                save(r, t)              # failures stay uncached -> retried
                pdf_ok += 1
            else:
                failed += 1
            rows.append(r)
        if winner[0]["text_chars"] == 0:
            no_text += 1
        elif winner[0]["variant"] == "base":
            kept_base += 1
        else:
            kept_beschluss += 1

    out = snapshot_dir("br_texts")
    n = write_jsonl(out / "index.jsonl", rows)
    print(f"\n[done] targets={len(targets)} requests={reqs} "
          f"cache_hits={cache_hits} pdf_ok={pdf_ok} failed={failed} "
          f"kept_base={kept_base} kept_beschluss={kept_beschluss} "
          f"no_text={no_text} budget_skipped={over_budget}")
    print(f"{n} index rows -> {out / 'index.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
