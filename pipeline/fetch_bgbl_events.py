"""Fetch promulgation LexEvents from the GII Aktualitätendienst RSS.

The feed is rebuilt daily and announces new BGBl I issues with their
recht.bund.de ELI links (verified 2026-07-06; ~215 items ≈ 8 months of
window). Each item becomes a `published` LexEvent — the signed-release
moment of the git model. ELI is the stable join key toward DIP
(vorgang.verkuendung[].pdf_url) and future BGBl article parsing.

Output (data/snapshots/bgbl_events/<date>/): events.jsonl
    {event_id, kind, actor, time, eli, bgbl_citation, title,
     legal_effect, source, fetched_at}
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from common import Http, snapshot_dir, write_jsonl

FEED = "https://www.gesetze-im-internet.de/aktuDienst-rss-feed.xml"


def main() -> int:
    http = Http(delay=0.3)
    root = ET.fromstring(http.get(FEED, timeout=60).content)
    events = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        m = re.search(r"/eli/bund/([^/]+)/(\d{4})/(\d+)", link)
        eli = link if m else None
        try:
            when = parsedate_to_datetime(pub).date().isoformat()
        except Exception:                      # noqa: BLE001
            when = None
        cite = title if title.startswith("BGBl") else None
        events.append({
            "event_id": f"event:{m.group(1)}:{m.group(2)}-{m.group(3)}"
                        f":promulgation" if m else f"event:rss:{hash(link)}",
            "kind": "published",
            "actor": "BGBl / recht.bund.de",
            "time": when,
            "eli": eli,
            "bgbl_citation": cite,
            "title": desc or title,
            "legal_effect": "publishes_law",
            "source": "gii_aktualitaetendienst",
            "fetched_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        })
    out = snapshot_dir("bgbl_events")
    write_jsonl(out / "events.jsonl", events)
    print(f"{len(events)} promulgation events -> {out}")
    for e in events[:4]:
        print(f"  {e['time']}  {e['bgbl_citation']}  "
              f"{(e['title'] or '')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
