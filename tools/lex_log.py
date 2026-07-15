"""`git log` for a German act — every layer of Lexgraph in one view.

    python3 tools/lex_log.py AsylbLG          # federal corpus
    python3 tools/lex_log.py AufnG            # Bavarian corpus
    python3 tools/lex_log.py "SGB 2" --all

Federal sections: HEAD (GII), pending patches in the Bundestag pipeline
(DIP, status ladder). Private Buzer candidates are shown only with the explicit
``LEXGRAPH_INCLUDE_QUARANTINED=1`` research switch.
Bavarian sections: HEAD (BAYERN.RECHT XML), Landtag WP19 bills touching
the act, GVBl/BayMBl promulgations (BayRS-Gliederungsnummer join),
back-history (ffn Fortführungsnachweis + XML aenderungsverlauf).
A recommended-but-unmerged patch shows up under its real status and
never as geltendes Recht — the VISION acceptance discipline.
"""
from __future__ import annotations

import argparse
import os
import re as _re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from common import latest_snapshot, read_jsonl          # noqa: E402

BADGE = {"proposed": "○ proposed", "adopted": "◑ adopted",
         "published": "● published", "rejected": "✗ rejected",
         "not_merged": "✗ not merged"}
BY_BADGE = {"eingebracht": "○ eingebracht", "1_lesung": "○ 1. Lesung",
            "ausschuss": "○ Ausschuss", "2_lesung": "◑ 2. Lesung",
            "beschlossen": "◑ beschlossen", "verkuendet": "● verkündet",
            "abgelehnt": "✗ abgelehnt", "zurueckgezogen": "✗ zurückgez."}
# title needles for Landtag bill -> corpus act matching (same set as
# tools/build_qfs.py)
BY_NEEDLES = {
    "AufnG": "aufnahmegesetz", "DVAsyl": "asyldurchführungsverordnung",
    "BayIntG": "integrationsgesetz",
    "AGSG": "ausführung der sozialgesetze",
    "BayVwVfG": "verwaltungsverfahrensgesetz",
    "AGVwGO": "ausführung der verwaltungsgerichtsordnung",
    "VwZVG": "verwaltungszustellungs",
    "LStVG": "landesstraf- und verordnungsgesetz",
    "PAG": "polizeiaufgabengesetz",
    "BayEUG": "erziehungs- und unterrichtswesen",
}


def load(source: str, name: str) -> list[dict]:
    snap = latest_snapshot(source)
    return list(read_jsonl(snap / name)) if snap else []


def include_private_candidates() -> bool:
    return os.environ.get("LEXGRAPH_INCLUDE_QUARANTINED") == "1"


def pick_act(query: str, acts: list[dict]) -> dict | None:
    q = query.lower().replace("_", " ").strip()
    for a in acts:
        if a["jurabk"].lower() == q or a.get("slug") == query.lower():
            return a
    for a in acts:
        if q in a["jurabk"].lower() or q in (a.get("long_title") or "").lower():
            return a
    return None


def federal_log(act: dict, show_all: bool) -> None:
    jb = act["jurabk"]
    print(f"== {jb} — {act.get('long_title') or ''}".rstrip())
    print(f"HEAD  GII build {act.get('builddate', '')[:8]}  "
          f"{act.get('norm_count')} §§")
    if act.get("stand"):
        print(f"      {act['stand'][:110]}")

    patches = [p for p in load("patches", "patches.jsonl")
               if p["target_act"] == jb]
    live = [p for p in patches if p["status"] in ("proposed", "adopted")]
    done = [p for p in patches if p["status"] == "published"]
    dead = [p for p in patches if p["status"] in ("rejected", "not_merged")]

    if live:
        print(f"\n-- pipeline: {len(live)} pending patch(es) "
              f"(NOT geltendes Recht)")
        seen = set()
        for p in live:
            k = p["procedure"]
            if k in seen:
                continue
            seen.add(k)
            n = sum(1 for x in live if x["procedure"] == k)
            paras = sorted({x["ref"].get("para") for x in live
                            if x["procedure"] == k and x["ref"].get("para")},
                           key=lambda v: (len(v), v))
            tgt = (" §§ " + ", ".join(paras[:8])) if paras else ""
            print(f"  {BADGE[p['status']]:12} {n:3} cmd(s){tgt}")
            print(f"               {p['procedure_title'][:88]}")
            print(f"               [{p['source_doc']}] "
                  f"{p['beratungsstand'] or ''}")
    if done:
        vg = {p["procedure"] for p in done}
        print(f"\n-- promulgated procedures: {len(done)} draft patch "
              f"candidate(s) from {len(vg)} procedure(s); final-text "
              "attribution not proven here")
    if dead:
        vg = {p["procedure"] for p in dead}
        print(f"-- killed: {len(dead)} patch cmd(s) from {len(vg)} "
              f"rejected/withdrawn bills (history, zero effect)")

    bz = latest_snapshot("buzer") if include_private_candidates() else None
    if bz:
        up = load("buzer", "upcoming.jsonl")
        vs = [v for v in load("buzer", "versions.jsonl")
              if v["jurabk"] == jb]
        ids = {v["act_id"] for v in vs}
        mine = [u for u in up if u.get("act_id") in ids]
        if mine:
            print("\n-- promulgated, enters force soon:")
            for u in mine:
                print(f"  ⏳ {u['date']}  {u['title'][:80]}")
        if vs:
            vs.sort(key=lambda v: v["date"], reverse=True)
            show = vs if show_all else vs[:10]
            print(f"\n-- back-history ({len(vs)} versions since "
                  f"{vs[-1]['date'][:4]}, buzer — non-authoritative):")
            for v in show:
                t = v["title"]
                t = t.split("geändert durch")[-1].strip() \
                    if "geändert durch" in t else t
                t = _re.sub(r"^\d{2}\.\d{2}\.\d{4}\s*", "", t)
                t = t.replace("Synopse gesamt oder einzeln für", "§§") \
                     .replace("Synopse gesamt", "").strip()
                print(f"  {v['date']}  {t[:86]}")
            if not show_all and len(vs) > 10:
                print(f"  … {len(vs) - 10} more (--all)")


def bavaria_log(act: dict, show_all: bool) -> None:
    jb = act["jurabk"]
    print(f"== BY {jb} — {act.get('long_title') or ''}".rstrip())
    print(f"HEAD  BAYERN.RECHT build {act.get('builddate', '')[:10]}  "
          f"{act.get('norm_count')} Art./§§  BayRS {act.get('bayrs_nr')}")
    print(f"      https://www.gesetze-bayern.de/Content/Document/"
          f"{act.get('key')}")

    needle = BY_NEEDLES.get(jb, "")
    bills = [b for b in load("bay_landtag", "bills.jsonl")
             if needle and needle in (b.get("titel") or "").lower()]
    live = [b for b in bills if b["status"] not in
            ("verkuendet", "abgelehnt", "zurueckgezogen")]
    done = [b for b in bills if b["status"] == "verkuendet"]
    dead = [b for b in bills if b["status"] in
            ("abgelehnt", "zurueckgezogen")]
    if live:
        print(f"\n-- Landtag WP19 pipeline: {len(live)} pending bill(s) "
              f"(NOT geltendes Recht)")
        for b in live:
            print(f"  {BY_BADGE.get(b['status'], b['status']):15} "
                  f"[{b['drs_nr']}] {b['titel'][:70]}")
            print(f"                  {', '.join(b.get('initiators') or [])}"
                  f"  eingebracht {b.get('eingang')}")
    if done:
        for b in done:
            print(f"\n-- verkündet: [{b['drs_nr']}] {b['titel'][:68]}")
            print(f"     {b.get('gvbl_citation') or ''}")
    if dead:
        print(f"-- killed: {len(dead)} bill(s) "
              f"({', '.join(b['drs_nr'] for b in dead[:6])})")

    bayrs = act.get("bayrs_nr") or "—"
    ev = [e for e in load("gvbl_events", "events.jsonl")
          if bayrs in [x.strip() for x in
                       (e.get("gliederungs_nr") or "").split(",")]]
    if ev:
        print("\n-- GVBl/BayMBl (joined via BayRS Gliederungsnr):")
        for e in ev:
            print(f"  {e['time']}  [{e['gazette']}, {e['authenticity']}] "
                  f"{e['title'][:64]}")
            print(f"              {e['permalink']}")

    vs = [v for v in load("bayern_recht", "versions.jsonl")
          if v["jurabk"] == jb and v.get("date")]
    seen, uniq = set(), []
    for v in sorted(vs, key=lambda x: 0 if x.get("source") == "xml" else 1):
        # cross-source dedupe only — distinct same-date amendments differ
        # in citation/description and must both stay visible; ffn and xml
        # cite the same GVBl page in different formats, so key on the page
        m = _re.search(r"S\.\s*(\d+)", v.get("gvbl_citation") or "")
        k = (v["date"], m.group(1) if m else v["description"][:40])
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    uniq.sort(key=lambda v: v["date"], reverse=True)
    if uniq:
        show = uniq if show_all else uniq[:10]
        print(f"\n-- back-history ({len(uniq)} versions since "
              f"{uniq[-1]['date'][:4]}, amtlich: ffn/XML):")
        for v in show:
            cite = f"  ({v['gvbl_citation']})" if v.get("gvbl_citation") \
                else ""
            print(f"  {v['date']}  {v['description'][:70]}{cite}")
        if not show_all and len(uniq) > 10:
            print(f"  … {len(uniq) - 10} more (--all)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("act", help="jurabk, GII slug or title fragment "
                               "(federal or Bavarian corpus)")
    ap.add_argument("--all", action="store_true",
                    help="full back-history (default: last 10 versions)")
    args = ap.parse_args()

    fed = load("gii", "acts.jsonl")
    bay = load("bayern_recht", "acts.jsonl")
    act = pick_act(args.act, fed)
    if act:
        federal_log(act, args.all)
        return 0
    act = pick_act(args.act, bay)
    if act:
        bavaria_log(act, args.all)
        return 0
    print(f"no corpus act matches {args.act!r} "
          f"(federal: {len(fed)}, bavarian: {len(bay)})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
