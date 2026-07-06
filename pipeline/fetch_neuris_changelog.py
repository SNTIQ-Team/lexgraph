"""Archive the NeuRIS legislation changelog as LexEvents.

NeuRIS (testphase.rechtsinformationen.bund.de) is the only source serving
consolidated federal law as LegalDocML with temporal ELIs — but expressions
VANISH from its search index when superseded, while their manifestation
URLs keep working. So the changelog is not just a delta feed, it is the
only enumeration of version history that exists: every run appends to a
cumulative archive (data/neuris_archive.jsonl) that must never shrink.

Endpoint (audited 2026-07-06, only /v1 is robots-sanctioned):
    GET /v1/legislation/changelog?from=<iso>&to=<iso>
    -> {"changed":[{contentUrl}...], "deleted":[...], "allChanged":bool}

ELI anatomy in contentUrl:
    /v1/legislation/eli/bund/bgbl-1/1957/s652/2025-01-01/1/deu/2024-01-23.zip
        work ELI ----------^^^^^^^^  point-in-time^  ver lang  manifested^
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

from common import ROOT, Http, latest_snapshot, read_jsonl, snapshot_dir, \
    write_jsonl

BASE = "https://testphase.rechtsinformationen.bund.de"
ARCHIVE = ROOT / "data" / "neuris_archive.jsonl"

ELI_RE = re.compile(
    r"(?P<work>eli/bund/[^/]+/\d{4}/[^/]+)"
    r"/(?P<pit>\d{4}-\d{2}-\d{2})/(?P<ver>\d+)/(?P<lang>[a-z]{3})"
    r"(?:/(?P<manifested>\d{4}-\d{2}-\d{2}))?")


def to_event(url: str, kind: str, fetched_at: str) -> dict:
    m = ELI_RE.search(url)
    d = m.groupdict() if m else {}
    return {
        "event_id": f"event:neuris:{kind}:{url.rsplit('/v1/', 1)[-1]}",
        "kind": kind,                # consolidation_changed | _deleted
        "actor": "NeuRIS (BMJ/DigitalService)",
        "time": d.get("manifested") or d.get("pit"),
        "eli_work": d.get("work"),
        "point_in_time": d.get("pit"),      # expression validity start
        "expression_version": d.get("ver"),
        "content_url": url if url.startswith("http") else BASE + url,
        "legal_effect": "revises_consolidation",
        "source": "neuris_changelog",
        "fetched_at": fetched_at,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="window length; default: since previous snapshot")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    prev = latest_snapshot("neuris_changelog")
    if args.days:
        since = now - timedelta(days=args.days)
    elif prev:                      # overlap 1 day; archive dedupes
        y, m, dd = map(int, prev.name.split("-"))
        since = datetime(y, m, dd, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        since = now - timedelta(days=30)

    http = Http(delay=0.5)
    url = (f"{BASE}/v1/legislation/changelog"
           f"?from={since:%Y-%m-%dT%H:00:00Z}&to={now:%Y-%m-%dT%H:00:00Z}")
    r = http.get(url, timeout=120)
    if r.status_code != 200:
        print(f"changelog HTTP {r.status_code}", file=sys.stderr)
        return 1
    data = r.json()
    fetched_at = now.isoformat(timespec="seconds")

    events = [to_event(c.get("contentUrl", ""), "consolidation_changed",
                       fetched_at) for c in data.get("changed", [])]
    events += [to_event(c.get("contentUrl", ""), "consolidation_deleted",
                        fetched_at) for c in data.get("deleted", [])]
    events = [e for e in events if e["eli_work"]]

    out = snapshot_dir("neuris_changelog")
    write_jsonl(out / "events.jsonl", events)

    # append-only cumulative archive (the URLs are unrecoverable later)
    known = ({e["event_id"] for e in read_jsonl(ARCHIVE)}
             if ARCHIVE.exists() else set())
    fresh = [e for e in events if e["event_id"] not in known]
    with open(ARCHIVE, "a", encoding="utf-8") as f:
        import json
        for e in fresh:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    works = {e["eli_work"] for e in events}
    print(f"window {since:%Y-%m-%d} -> {now:%Y-%m-%d}: "
          f"{len(events)} events over {len(works)} works "
          f"(allChanged={data.get('allChanged')})")
    print(f"  snapshot -> {out}")
    print(f"  archive  +{len(fresh)} (total {len(known) + len(fresh)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
