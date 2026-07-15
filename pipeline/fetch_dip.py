"""Fetch the live legislative pipeline from the DIP Bundestag API.

DIP is the PRIMARY anticipation source (verified 2026-07-06): every
Gesetzgebung procedure with beratungsstand, initiative, sachgebiet,
verkuendung[] (BGBl citation + recht.bund.de ELI) and inkrafttreten[].
Updates are intraday; delta sync via f.aktualisiert.start.

The API requires the officially published public key; it rotates, so on
401 we re-read it from the openapi spec instead of hardcoding trust.

Output (data/snapshots/dip/<date>/):
    vorgaenge.jsonl  all WP legislative procedures
    positions.jsonl  official document/stage chain for explicit DIP watches

Usage:
    python3 pipeline/fetch_dip.py [--wahlperiode 21] [--since ISO]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from common import ROOT, Http, snapshot_dir, write_jsonl

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


def watched_dip_configs() -> dict[str, dict]:
    """Load explicit DIP watches without coupling the generic API crawl."""
    path = ROOT / "data" / "procedure_watchlist.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(key): value for key, value in (payload.get("procedures") or {}).items()
        if isinstance(value, dict) and
        str(value.get("source") or "DIP").casefold() == "dip" and
        bool(value.get("monitor", True))
    }


def _text_validation(http: Http, key: str, position: dict,
                     check: dict) -> dict:
    """Run a configured assertion against official DIP document plaintext.

    Only the match result and compact excerpts are stored.  The analysis step
    remains offline and reproducible, while every refresh rechecks the claim
    against the official document instead of trusting a prose summary.
    """
    result = {
        "id": check.get("id"),
        "kind": check.get("kind") or "operative_text",
        "label": check.get("label"),
        "finding": check.get("finding"),
        "document_number": (position.get("fundstelle") or {}).get(
            "dokumentnummer"),
        "source_url": check.get("source_url") or
                      (position.get("fundstelle") or {}).get("pdf_url"),
    }
    document_id = (position.get("fundstelle") or {}).get("id")
    if not document_id:
        result.update({"passed": False, "retrieval_status": "no_document_id",
                       "patterns": []})
        return result
    response = http.get(f"{BASE}/drucksache-text/{document_id}",
                        params={"apikey": key}, timeout=90)
    if response.status_code != 200:
        result.update({"passed": False,
                       "retrieval_status": f"http_{response.status_code}",
                       "patterns": []})
        return result
    text = str(response.json().get("text") or "")
    patterns = []
    for expression in check.get("required_patterns") or []:
        try:
            match = re.search(str(expression), text,
                              flags=re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            patterns.append({"pattern": expression, "matched": False,
                             "error": str(exc)})
            continue
        excerpt = None
        if match:
            excerpt = " ".join(
                text[max(0, match.start() - 100):match.end() + 180].split())
        patterns.append({"pattern": expression, "matched": bool(match),
                         "excerpt": excerpt})
    result.update({
        "passed": bool(patterns) and all(item.get("matched")
                                         for item in patterns),
        "retrieval_status": "fetched",
        "patterns": patterns,
    })
    return result


def _official_evidence_position(http: Http, watch_key: str,
                                check: dict) -> dict:
    """Verify one official Bundestag evidence page as a synthetic position."""
    url = str(check.get("url") or "")
    validation = {
        "id": check.get("id"),
        "kind": "official_event",
        "label": check.get("label"),
        "finding": check.get("label"),
        "source_url": url,
    }
    try:
        response = http.get(url, timeout=60)
        response.raise_for_status()
        html_text = response.text
        patterns = []
        for expression in check.get("required_patterns") or []:
            match = re.search(str(expression), html_text,
                              flags=re.IGNORECASE | re.MULTILINE)
            patterns.append({"pattern": expression, "matched": bool(match)})
        validation.update({
            "passed": bool(patterns) and all(item["matched"]
                                             for item in patterns),
            "retrieval_status": "fetched",
            "patterns": patterns,
        })
    except Exception as exc:  # network evidence failure stays explicit
        validation.update({"passed": False,
                           "retrieval_status": type(exc).__name__,
                           "patterns": []})
    return {
        "id": f"evidence-{check.get('id')}",
        "watch_key": watch_key,
        "vorgang_id": watch_key,
        "vorgangsposition": check.get("label"),
        "zuordnung": "BT",
        "dokumentart": "Official evidence page",
        "datum": check.get("date"),
        "fundstelle": {
            "dokumentnummer": check.get("id"),
            "dokumentart": "Official evidence page",
            "herausgeber": "BT",
            "pdf_url": url,
        },
        "content_validations": [validation],
    }


def fetch_watched_positions(http: Http, key: str,
                            configs: dict[str, dict]) -> list[dict]:
    """Fetch complete official position chains for active DIP watches."""
    rows: list[dict] = []
    for watch_key, config in configs.items():
        cursor = None
        procedure_rows: list[dict] = []
        while True:
            params = {"f.vorgang": watch_key, "apikey": key}
            if cursor:
                params["cursor"] = cursor
            response = http.get(f"{BASE}/vorgangsposition", params=params,
                                timeout=60)
            if response.status_code == 401:
                key = current_key(http)
                params["apikey"] = key
                response = http.get(f"{BASE}/vorgangsposition", params=params,
                                    timeout=60)
            response.raise_for_status()
            payload = response.json()
            documents = payload.get("documents") or []
            procedure_rows.extend(documents)
            next_cursor = payload.get("cursor")
            if not documents or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        configured_checks = config.get("content_checks") or []
        for position in procedure_rows:
            position = dict(position)
            position["watch_key"] = watch_key
            number = str((position.get("fundstelle") or {}).get(
                "dokumentnummer") or "")
            validations = [
                _text_validation(http, key, position, check)
                for check in configured_checks
                if isinstance(check, dict) and
                str(check.get("document_number") or "") == number
            ]
            if validations:
                position["content_validations"] = validations
            rows.append(position)
        evidence = [
            _official_evidence_position(http, watch_key, check)
            for check in config.get("official_evidence_checks") or []
            if isinstance(check, dict) and check.get("url")
        ]
        rows.extend(evidence)
        print(f"  watch {watch_key}: {len(procedure_rows)} positions + "
              f"{len(evidence)} verified evidence pages")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wahlperiode", type=int, default=21)
    ap.add_argument("--since", help="ISO date for delta sync")
    args = ap.parse_args()

    http = Http(delay=0.4)
    key = current_key(http)
    rows = fetch_vorgaenge(http, key, args.wahlperiode, args.since)
    positions = fetch_watched_positions(http, key, watched_dip_configs())
    out = snapshot_dir("dip")
    write_jsonl(out / "vorgaenge.jsonl", rows)
    write_jsonl(out / "positions.jsonl", positions)
    from collections import Counter
    stands = Counter(r.get("beratungsstand") or "?" for r in rows)
    print(f"vorgaenge: {len(rows)} -> {out}")
    print(f"watched positions: {len(positions)} -> {out}")
    for k, n in stands.most_common(8):
        print(f"  {n:4} {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
