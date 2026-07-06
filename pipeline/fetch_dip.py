"""Fetch the live legislative pipeline from the DIP Bundestag API.

DIP is the PRIMARY anticipation source (verified 2026-07-06): every
Gesetzgebung procedure with beratungsstand, initiative, sachgebiet,
verkuendung[] (BGBl citation + recht.bund.de ELI) and inkrafttreten[].
Updates are intraday; delta sync via f.aktualisiert.start.

The API requires the officially published public key; it rotates, so on
401 we re-read it from the openapi spec instead of hardcoding trust.

Output (data/snapshots/dip/<date>/): vorgaenge.jsonl

Usage:
    python3 pipeline/fetch_dip.py [--wahlperiode 21] [--since ISO]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from common import Http, snapshot_dir, write_jsonl

BASE = "https://search.dip.bundestag.de/api/v1"
# officially published public key (from /api/v1/openapi.yaml, 2026-07)
PUBLIC_KEY = "R2BZaee.DjdCyihKZMf8AOjtScubP2EVydegzjmBIQ"


def current_key(http: Http) -> str:
    """Re-read the rotating public key from the official spec."""
    r = http.get(f"{BASE}/openapi.yaml", timeout=30)
    m = re.search(r"apikey=([A-Za-z0-9._\-]+)", r.text)
    return m.group(1) if m else PUBLIC_KEY


def fetch_vorgaenge(http: Http, key: str, wahlperiode: int,
                    since: str | None) -> list[dict]:
    rows, cursor = [], None
    while True:
        params = {"f.vorgangstyp": "Gesetzgebung",
                  "f.wahlperiode": wahlperiode, "apikey": key}
        if since:
            params["f.aktualisiert.start"] = since
        if cursor:
            params["cursor"] = cursor
        r = http.get(f"{BASE}/vorgang", params=params, timeout=45)
        if r.status_code == 401:                 # key rotated
            key = current_key(http)
            params["apikey"] = key
            r = http.get(f"{BASE}/vorgang", params=params, timeout=45)
        r.raise_for_status()
        d = r.json()
        docs = d.get("documents", [])
        rows.extend(docs)
        nxt = d.get("cursor")
        if not docs or not nxt or nxt == cursor:
            break
        cursor = nxt
        if len(rows) % 500 == 0:
            print(f"  {len(rows)}/{d.get('numFound', '?')}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wahlperiode", type=int, default=21)
    ap.add_argument("--since", help="ISO date for delta sync")
    args = ap.parse_args()

    http = Http(delay=0.4)
    key = current_key(http)
    rows = fetch_vorgaenge(http, key, args.wahlperiode, args.since)
    out = snapshot_dir("dip")
    write_jsonl(out / "vorgaenge.jsonl", rows)
    from collections import Counter
    stands = Counter(r.get("beratungsstand") or "?" for r in rows)
    print(f"vorgaenge: {len(rows)} -> {out}")
    for k, n in stands.most_common(8):
        print(f"  {n:4} {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
