"""Extract PatchInstructions from WP21 Gesetzentwürfe (DIP full texts).

For every Vorgang whose title resolves to a practice-corpus act, fetch
the Gesetzentwurf Drucksache text and parse its Änderungsbefehle
(patch_parser). Lifecycle comes from the Vorgang's beratungsstand — the
status ladder makes "recommended-but-never-merged" queryable, which is
the whole point (VISION acceptance test).

Drucksache texts are immutable once printed -> cached forever in
data/cache/dip_text/{id}.txt; re-runs only hit the API for new bills.

Output (data/snapshots/patches/<date>/patches.jsonl): one row per
command, VISION PatchInstruction shape + ref/operation/raw.
"""
from __future__ import annotations

import json
import sys
from datetime import date

from common import ROOT, Http, latest_snapshot, read_jsonl, snapshot_dir, \
    write_jsonl
from fetch_dip import BASE, current_key
from patch_parser import parse_bill, resolve_act

CACHE = ROOT / "data" / "cache" / "dip_text"

# beratungsstand -> PatchInstruction.status
LADDER = {
    "Verkündet": "published",
    "Verabschiedet": "adopted",
    "Abgelehnt": "rejected",
    "Für erledigt erklärt": "not_merged",
    "Zurückgezogen": "not_merged",
}


def bill_text(http: Http, key: str, doc_id: str) -> str:
    f = CACHE / f"{doc_id}.txt"
    if f.exists():
        return f.read_text()
    r = http.get(f"{BASE}/drucksache-text/{doc_id}?apikey={key}",
                 timeout=90)
    text = (r.json().get("text") or "") if r.status_code == 200 else ""
    if text:                # empty = transient 404/not-yet-OCRed: retry
        CACHE.mkdir(parents=True, exist_ok=True)     # on the next run
        f.write_text(text)
    return text


BR_CACHE = ROOT / "data" / "cache" / "br_text"


def br_fallback(http: Http, key: str, cover: str) -> str:
    """Bundesrat-initiated bills reach DIP as a 2-page transmittal letter
    that only CITES the BR-Drucksache ('Drucksache 173/24 (Beschluss)').
    Best source: the bundesrat.de PDF text cache filled by
    fetch_br_texts.py (the (B) variant carries the adopted full text).
    Fallback: DIP's own BR document — usually another cover letter."""
    import re
    texts = []
    for m in re.finditer(r"Drucksache\s+(\d+)/(\d+)", cover):
        nr, yy = m.group(1), m.group(2)
        if not (20 <= int(yy) <= 26):
            continue
        for suffix in ("B", ""):             # Beschluss text first
            f = BR_CACHE / f"{nr}-{yy}{suffix}.txt"
            if f.is_file():
                texts.append(f.read_text())
    if texts:                                # longest substantive wins
        best = max(texts, key=len)
        if len(best) > 2500:
            return best
    m = re.search(r"Drucksache\s+(\d+/\d+)", cover)
    if not m:
        return ""
    r = http.get(f"{BASE}/drucksache?f.dokumentnummer={m.group(1)}"
                 f"&apikey={key}", timeout=60)
    if r.status_code != 200:
        return ""
    for d in r.json().get("documents", []):
        if d.get("herausgeber") == "BR":
            return bill_text(http, key, str(d["id"]))
    return ""


def entwurf_of(http: Http, key: str, vorgang_id) -> dict | None:
    """Earliest Gesetzentwurf position of a Vorgang -> fundstelle."""
    r = http.get(f"{BASE}/vorgangsposition?f.vorgang={vorgang_id}"
                 f"&apikey={key}", timeout=60)
    if r.status_code != 200:
        return None
    docs = [p for p in r.json().get("documents", [])
            if (p.get("fundstelle") or {}).get("drucksachetyp")
            == "Gesetzentwurf"]
    docs.sort(key=lambda p: p.get("datum") or "9999")
    return docs[0]["fundstelle"] if docs else None


def main() -> int:
    dip = latest_snapshot("dip")
    gii = latest_snapshot("gii")
    if not (dip and gii):
        print("run fetch_dip.py / fetch_gii.py first", file=sys.stderr)
        return 1
    acts = list(read_jsonl(gii / "acts.jsonl"))
    vorgaenge = list(read_jsonl(dip / "vorgaenge.jsonl"))
    # no title pre-filter: omnibus bills hide corpus amendments behind
    # unrelated titles ("Investitionssofortprogramm" -> EStG). Fetch all,
    # decide per Artikel; texts are cached forever, so this is cheap on
    # every run after the first.
    print(f"scanning all {len(vorgaenge)} Vorgänge (Artikel-level "
          f"corpus resolution)")

    http = Http(delay=0.4)
    key = current_key(http)
    rows, no_text, cover_only, off_corpus = [], 0, 0, 0
    for vg in vorgaenge:
        fs = entwurf_of(http, key, vg["id"])
        if not fs:
            no_text += 1
            continue
        text = bill_text(http, key, str(fs.get("id")))
        parsed = parse_bill(text, acts)
        if not parsed["patches"] and len(text) < 4000:
            text = br_fallback(http, key, text)
            parsed = parse_bill(text, acts)
        if not parsed["patches"] and len(text) < 4000:
            # BR-initiated bill: DIP carries only transmittal letters,
            # the substance is a bundesrat.de PDF — known source gap
            cover_only += 1
            if resolve_act(vg.get("titel") or "", acts):
                print(f"  vg {vg['id']} [cover-only] BR text not in DIP"
                      f"  {(vg.get('titel') or '')[:56]}")
            continue
        if len(text) < 500:
            no_text += 1
            continue
        status = LADDER.get(vg.get("beratungsstand") or "", "proposed")
        ikt = parsed["inkrafttreten"]
        for p in parsed["patches"]:
            jurabk = p.pop("target_act")
            if not jurabk:            # Artikel amends a non-corpus act
                off_corpus += 1
                continue
            ref = p.pop("ref")
            slug = jurabk.lower().replace(" ", "_").replace("/", "_")
            target = f"norm:de.bund.{slug}"
            if ref.get("para"):
                target += f".p{ref['para']}"
            rows.append({
                "patch_id": f"patch:dip:{vg['id']}"
                            f":a{p['artikel']}.n{p['item']}",
                "target": target,
                "target_act": jurabk,
                "ref": ref,
                "operation": p["operation"],
                "old_text_constraint": p["old_text_constraint"],
                "new_text": p["new_text"],
                "status": status,
                "merged": status == "published",
                "source_doc": f"bt-ds:{fs.get('dokumentnummer')}",
                "procedure": f"dip-vorgang:{vg['id']}",
                "procedure_title": (vg.get("titel") or "")[:160],
                "beratungsstand": vg.get("beratungsstand"),
                "decided_at": vg.get("datum")
                              if status in ("adopted", "published") else None,
                "published_at": vg.get("datum")
                                if status == "published" else None,
                "valid_from": ikt.get("valid_from"),
                "in_force_mode": ikt.get("mode"),
                "raw": p["raw"],
            })
        mine = [r for r in rows
                if r['procedure'] == f"dip-vorgang:{vg['id']}"]
        if mine:
            tgts = sorted({r['target_act'] for r in mine})
            print(f"  vg {vg['id']} [{status:9}] {len(mine):3} cmds -> "
                  f"{', '.join(tgts[:4])}  "
                  f"{(vg.get('titel') or '')[:48]}")

    out = snapshot_dir("patches")
    write_jsonl(out / "patches.jsonl", rows)
    ops, sts = {}, {}
    for r in rows:
        ops[r["operation"]] = ops.get(r["operation"], 0) + 1
        sts[r["status"]] = sts.get(r["status"], 0) + 1
    print(f"\n{len(rows)} PatchInstructions -> {out}")
    print(f"  by operation: {json.dumps(ops, ensure_ascii=False)}")
    print(f"  by status:    {json.dumps(sts, ensure_ascii=False)}")
    print(f"  {off_corpus} commands for non-corpus acts dropped")
    if no_text or cover_only:
        print(f"  {no_text} without Entwurf text, {cover_only} "
              f"BR cover-letter-only (full text not in DIP)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
