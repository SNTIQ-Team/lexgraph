"""Export Lexgraph snapshots + arena into web/data/*.json for the local
visualizer (web/index.html). Pure read -> write, no network.

    python3 tools/build_web_data.py

Outputs:
    web/data/summary.json      stats + built_at (dashboard header, poll target)
    web/data/feed.json         merged event stream, newest first (~500)
    web/data/wiki.json         act index (federal + Bavaria)
    web/data/acts/<id>.json    per-act article: head, patches, versions, norms
    web/data/decisions.json    merged manual/RII decisions, newest first
    web/data/eu_index.json     in-force EU breadth metadata
    web/data/search.sqlite     ranked full-text index (acts + current norms)
    web/data/hierarchy.json    competence-aware legal layers (no geometry)
    web/data/graph.json        arena export: nodes/edges/beliefs/ticks/worlds
"""
from __future__ import annotations

import json
import re
import sys
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
from common import SNAPSHOTS, latest_snapshot, read_jsonl  # noqa: E402
from qfs import parse_qfs                                # noqa: E402
from api.search_engine import build_search_database       # noqa: E402

WEB = ROOT / "web" / "data"

HUBS = {"WIRD_GESETZ", "BGBl (verkündet)", "Gesetzgebungsverfahren",
        "GVBl/BayMBl (Bayern)", "TEXTÄNDERUNG (BayRS)",
        "TEXTÄNDERUNG in Kraft", "KONSOLIDIERT (NeuRIS)",
        "Amtsblatt der EU (OJ L)", "Asyl/Migration (Länder-Monitor)"}


# Cumulative word-diff ledger for Bavarian law.  BAYERN.RECHT serves only the
# current consolidation, so current changes come from adjacent daily snapshots;
# a conservative Wayback pass supplies sparse historical transitions where two
# archived states can be tied unambiguously to one official FFN event.
BY_DIFF_LEDGER = ROOT / "data" / "by_diffs.jsonl"


def _update_by_diff_ledger() -> None:
    base = SNAPSHOTS / "bayern_recht"
    days = sorted(d.name for d in base.iterdir() if d.is_dir()) \
        if base.is_dir() else []
    if len(days) < 2:
        return

    def norms(day: str) -> dict[tuple, str]:
        f = base / day / "norms.jsonl"
        if not f.is_file():
            return {}
        return {(n["jurabk"], n.get("enbez") or n.get("titel") or "?"):
                (n.get("text") or "") for n in read_jsonl(f)}

    old, new = norms(days[-2]), norms(days[-1])
    if not old or not new:
        return
    existing = set()
    if BY_DIFF_LEDGER.is_file():
        existing = {(r["jurabk"], r["date"], r["para"])
                    for r in read_jsonl(BY_DIFF_LEDGER)}
    # A newly curated act is not a newly enacted act.  Compare norms only for
    # acts present in both snapshots, while still retaining genuine norm-level
    # introductions and repeals inside those shared acts.
    shared_acts = {jurabk for jurabk, _ in old} & \
        {jurabk for jurabk, _ in new}
    added = []
    for key in sorted((set(old) | set(new))):
        jb, enbez = key
        if jb not in shared_acts:
            continue
        o, n = old.get(key, ""), new.get(key, "")
        if o == n or (jb, days[-1], enbez) in existing:
            continue
        added.append({"jurabk": jb, "date": days[-1], "para": enbez,
                      "old": o, "new": n,
                      "source": "daily_snapshot"})
    if added:
        with BY_DIFF_LEDGER.open("a", encoding="utf-8") as f:
            for r in added:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  by-diffs: +{len(added)} changed norms "
              f"({days[-2]} -> {days[-1]})")


def load_by_diffs() -> dict[str, dict[str, list[dict]]]:
    """{jurabk: {date: [{para, old, new}, …]}} from the ledger."""
    _update_by_diff_ledger()
    out: dict[str, dict[str, list[dict]]] = {}
    if BY_DIFF_LEDGER.is_file():
        for r in read_jsonl(BY_DIFF_LEDGER):
            out.setdefault(r["jurabk"], {}).setdefault(r["date"], []).append(
                {"para": r["para"], "old": r["old"], "new": r["new"]})
    return out


def build_eu_index() -> dict | None:
    """Breadth layer: every in-force EU directive + basic regulation as
    metadata rows (fetch_eu_index.py snapshot). None when never fetched."""
    snap = latest_snapshot("eu_index")
    if not snap or not (snap / "instruments.jsonl").is_file():
        return None
    rows = list(read_jsonl(snap / "instruments.jsonl"))
    return {"built_at": snap.name, "total": len(rows), "instruments": rows}


def load(source: str, name: str) -> list[dict]:
    snap = latest_snapshot(source)
    if not snap or not (snap / name).is_file():
        return []
    return list(read_jsonl(snap / name))


def load_decisions() -> list[dict]:
    """Manual high-value cases plus the cumulative official RII snapshot.

    Manual rows win when both sources describe the same court/date/docket,
    because they carry reviewed multilingual summaries and richer relations.
    Both inputs are optional; output is newest first.
    """
    f = ROOT / "data" / "decisions.json"
    manual = (json.loads(f.read_text(encoding="utf-8")).get("decisions") or []) \
        if f.is_file() else []
    automated = load("rii", "decisions.jsonl")

    def case_key(row: dict) -> tuple[str, str, str]:
        return (str(row.get("court_short") or "").casefold(),
                re.sub(r"\s+", "", str(row.get("az") or "")).casefold(),
                str(row.get("date") or ""))

    rows = list(manual)
    seen_ids = {str(row.get("id")) for row in manual if row.get("id")}
    seen_cases = {case_key(row) for row in manual}
    for row in automated:
        if str(row.get("id")) in seen_ids or case_key(row) in seen_cases:
            continue
        rows.append(row)
        if row.get("id"):
            seen_ids.add(str(row["id"]))
        seen_cases.add(case_key(row))
    rows.sort(key=lambda d: d.get("date") or "", reverse=True)
    return rows


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _para_sort(p: str) -> tuple:
    m = re.match(r"(\d+)([a-z]?)", p or "")
    return (int(m.group(1)), m.group(2)) if m else (10 ** 9, p or "")


def _clean_changes(changes: list | None) -> list:
    """De-noise scraped synopse changes: drop the act-title row and the
    Inhaltsübersicht heading-only duplicates, keep the fullest body per §,
    order by § number."""
    best: dict[str, dict] = {}
    for c in changes or []:
        para = c.get("para")
        old, new = c.get("old") or "", c.get("new") or ""
        if not para or not (old or new):
            continue                       # act-title / empty rows
        score = len(old) + len(new)
        if para not in best or score > best[para]["_s"]:
            best[para] = {"para": para, "old": old, "new": new, "_s": score}
    rows = sorted(best.values(), key=lambda c: _para_sort(c["para"]))
    for c in rows:
        c.pop("_s", None)
    return rows[:80]


def month_str(ord_: int) -> str:
    return f"{ord_ // 12:04d}-{ord_ % 12 + 1:02d}"


# ------------------------------------------------------------------ feed
def build_feed() -> list[dict]:
    rows: list[dict] = []

    def add(time, juris, source, kind, title, url=None, badge=None):
        if not time:
            return
        rows.append({"time": str(time)[:10], "juris": juris,
                     "source": source, "kind": kind,
                     "title": (title or "")[:160], "url": url,
                     "badge": badge})

    for e in load("bgbl_events", "events.jsonl"):
        add(e.get("time"), "DE", "BGBl", "verkündet",
            e.get("bgbl_citation") or e.get("title"), e.get("eli"))
    for e in load("gvbl_events", "events.jsonl"):
        add(e.get("time"), "DE-BY", e.get("gazette", "GVBl"), "verkündet",
            e.get("title"), e.get("permalink"), e.get("authenticity"))
    for e in load("eu_layer", "eu_events.jsonl"):
        add(e.get("time"), "EU", "OJ L", "veröffentlicht",
            (e.get("celex") or "") + "  " + (e.get("title") or ""))
    for e in load("bay_landtag", "events.jsonl"):
        add(str(e.get("time"))[:10], "DE-BY", "Landtag",
            e.get("kind", "drucksache"),
            f"{e.get('drs_nr', '')}  {e.get('title', '')}")
    lb_ids = set()
    for e in load("laender_bills", "bills.jsonl"):
        lb_ids.add(e.get("event_id"))
        add(e.get("datum"), e.get("jurisdiction", "DE-?"),
            "Landtag", "gesetzentwurf", e.get("titel"),
            (e.get("doc_urls") or [None])[0],
            "relevant" if e.get("relevant") else None)
    for e in load("laender_monitor", "events.jsonl"):
        if e.get("event_id") in lb_ids:      # already shown as bill
            continue
        add(e.get("datum"), e.get("jurisdiction", "DE-?"),
            "Parlamentsspiegel", "aktivität", e.get("titel"),
            (e.get("doc_urls") or [None])[0])
    for e in load("ep_layer", "ep_events.jsonl"):
        add(e.get("time"), "EU", "Europaparlament", "angenommen",
            e.get("title"), None,
            "relevant" if e.get("relevant") else None)
    for u in load("buzer", "upcoming.jsonl"):
        add(u.get("date"), "DE", "buzer", "tritt in Kraft ⏳",
            u.get("title"), u.get("url"))
    for d in load_decisions():
        add(d.get("date"), d.get("juris"), d.get("court_short"),
            "Entscheidung",
            (d.get("az") or "") + " — " + (d.get("title") or ""),
            d.get("url"))
    rows.sort(key=lambda r: r["time"], reverse=True)
    # court decisions are high-value — never let the 600-row cap
    # crowd them out: seat decisions first, fill the rest with the newest
    # other events, keep the merged window newest-first (the API caps
    # /feed at 600, so rescued rows must live INSIDE the window)
    decs = [r for r in rows if r["kind"] == "Entscheidung"]
    rest = [r for r in rows if r["kind"] != "Entscheidung"]
    head = decs + rest[:max(0, 600 - len(decs))]
    head.sort(key=lambda r: r["time"], reverse=True)
    return head


TODAY = date.today().isoformat()


def temporal(versions: list[dict], upcoming: list[dict],
             patches: list[dict]) -> dict:
    """Compact when-was / when-will-be summary for an act header.
    All dates ISO; the frontend formats to dd.mm.yyyy and localises.
    next_change counts only dates in the future (already-passed
    valid_from values would otherwise read as a fake upcoming change)."""
    past = sorted((v["date"] for v in versions if v.get("date")),
                  reverse=True)
    fut = [u["date"] for u in upcoming
           if u.get("date") and u["date"] > TODAY]
    fut += [p["valid_from"] for p in patches
            if p.get("valid_from") and p["valid_from"] > TODAY]
    pending = sum(1 for p in patches
                  if p["status"] in ("proposed", "adopted"))
    return {
        "last_change": past[0] if past else None,
        "first_change": past[-1] if past else None,
        "change_count": len(past),
        "next_change": min(fut) if fut else None,
        "pending": pending,
    }


# ------------------------------------------------------------------ wiki
def _act_decisions(decisions: list[dict], aid: str) -> list[dict]:
    """Minimal per-act projection of merged decisions: only rows whose
    effects touch this act, and only those effects (input is newest first)."""
    rows = []
    for d in decisions:
        eff = [e for e in d.get("effects") or [] if e.get("act_id") == aid]
        if eff:
            rows.append({"id": d["id"], "court_short": d.get("court_short"),
                         "level": d.get("level"), "az": d.get("az"),
                         "date": d.get("date"), "kind": d.get("kind"),
                         "title": d.get("title"), "effects": eff})
    return rows


def build_wiki() -> tuple[list[dict], dict[str, dict]]:
    idx, details = [], {}
    decisions = load_decisions()
    patches = load("patches", "patches.jsonl")
    buz_v = load("buzer", "versions.jsonl")
    buz_up = load("buzer", "upcoming.jsonl")
    # per-§ old/new text scraped from buzer synopse pages, keyed (act_id, date)
    syn_by_key: dict[tuple, list] = {}
    for s in load("buzer_synopse", "synopse.jsonl"):
        syn_by_key[(s["act_id"], s["date"])] = _clean_changes(s.get("changes"))
    gvbl = load("gvbl_events", "events.jsonl")
    by_v = load("bayern_recht", "versions.jsonl")
    bay_bills = load("bay_landtag", "bills.jsonl")
    by_diffs = load_by_diffs()

    for a in load("gii", "acts.jsonl"):
        jb = a["jurabk"]
        aid = "fed_" + slug(jb)
        pats = [p for p in patches if p["target_act"] == jb]
        ids = {v["act_id"] for v in buz_v if v["jurabk"] == jb}
        versions = []
        for v in sorted((v for v in buz_v if v["jurabk"] == jb),
                        key=lambda x: x["date"], reverse=True):
            t = v["title"]
            t = t.split("geändert durch")[-1].strip() \
                if "geändert durch" in t else t
            t = re.sub(r"^\d{2}\.\d{2}\.\d{4}\s*", "", t)
            t = t.replace("Synopse gesamt oder einzeln für", "§§") \
                 .replace("Synopse gesamt", "").strip()
            row = {"date": v["date"], "text": t[:300],
                   "url": v.get("synopsis_url")}
            ch = syn_by_key.get((v["act_id"], v["date"]))
            if ch:
                row["changes"] = ch
            versions.append(row)
        upcoming = [{"date": u["date"], "title": u["title"][:120],
                     "url": u.get("url")}
                    for u in buz_up if u.get("act_id") in ids]
        patch_rows = [{
            "status": p["status"], "op": p["operation"],
            "para": p["ref"].get("para"),
            "absatz": p["ref"].get("absatz"),
            "proc": p["procedure_title"][:120],
            "doc": p["source_doc"], "stand": p.get("beratungsstand"),
            "valid_from": p.get("valid_from"),
            "old": (p.get("old_text_constraint") or "")[:400] or None,
            "new": (p.get("new_text") or "")[:400] or None,
        } for p in pats]
        _tmp = temporal(versions, upcoming, patch_rows)
        decs = _act_decisions(decisions, aid)
        idx.append({"id": aid, "jurabk": jb, "juris": "DE",
                    "title": a.get("long_title") or jb,
                    "norms": a.get("norm_count"),
                    "build": a.get("builddate", "")[:8],
                    "last_change": _tmp["last_change"],
                    "next_change": _tmp["next_change"],
                    "pending": _tmp["pending"],
                    "decisions": len(decs)})
        details[aid] = {
            "id": aid, "jurabk": jb, "juris": "DE",
            "title": a.get("long_title"), "stand": a.get("stand"),
            "build": a.get("builddate", "")[:8],
            "norm_count": a.get("norm_count"),
            "patches": patch_rows,
            "upcoming": upcoming,
            "versions": versions,
            "temporal": temporal(versions, upcoming, patch_rows),
            "norms": [],
        }
        if decs:
            details[aid]["decisions"] = decs

    for a in load("bayern_recht", "acts.jsonl"):
        jb = a["jurabk"]
        aid = "by_" + slug(jb)
        seen, versions = set(), []
        for v in sorted((v for v in by_v
                         if v["jurabk"] == jb and v.get("date")),
                        key=lambda x: (x["date"],
                                       0 if x["source"] == "xml" else 1),
                        reverse=True):
            m = re.search(r"S\.\s*(\d+)", v.get("gvbl_citation") or "")
            k = (v["date"], m.group(1) if m else v["description"][:40])
            if k in seen:
                continue
            seen.add(k)
            cite = f" ({v['gvbl_citation']})" if v.get("gvbl_citation") \
                else ""
            versions.append({"date": v["date"],
                             "text": (v["description"][:110] + cite)})
        # attach snapshot-derived word diffs: onto the matching GVBl row
        # when the dates line up, else as their own version row
        for d, chs in (by_diffs.get(jb) or {}).items():
            row = next((r for r in versions if r["date"] == d), None)
            if row is None:
                row = {"date": d, "text":
                       "Konsolidierte Fassung geändert (BAYERN.RECHT)"}
                versions.append(row)
            row["changes"] = chs
        versions.sort(key=lambda r: r["date"], reverse=True)
        bills = [b for b in bay_bills
                 if _by_needle(jb) and _by_needle(jb) in
                 (b.get("titel") or "").lower()]
        _tmp = temporal(versions, [], [])
        decs = _act_decisions(decisions, aid)
        idx.append({"id": aid, "jurabk": jb, "juris": "DE-BY",
                    "title": a.get("long_title") or jb,
                    "norms": a.get("norm_count"),
                    "build": a.get("builddate", "")[:10],
                    "last_change": _tmp["last_change"],
                    "next_change": _tmp["next_change"], "pending": 0,
                    "decisions": len(decs)})
        details[aid] = {
            "id": aid, "jurabk": jb, "juris": "DE-BY",
            "title": a.get("long_title"), "bayrs": a.get("bayrs_nr"),
            "build": a.get("builddate", "")[:10],
            "norm_count": a.get("norm_count"),
            "permalink": "https://www.gesetze-bayern.de/Content/Document/"
                         + (a.get("key") or ""),
            "bills": [{"status": b["status"], "drs": b["drs_nr"],
                       "title": b["titel"][:120],
                       "gvbl": b.get("gvbl_citation")} for b in bills],
            "gvbl_events": [
                {"date": e["time"], "gazette": e["gazette"],
                 "title": e["title"][:110], "url": e["permalink"]}
                for e in gvbl
                if (a.get("bayrs_nr") or "†") in
                [x.strip() for x in
                 (e.get("gliederungs_nr") or "").split(",")]],
            # no DIP patch stream for Bavarian acts — ship the keys anyway
            # so every act detail has one shape (clients iterate patches)
            "patches": [], "upcoming": [],
            "versions": versions,
            "temporal": temporal(versions, [], []),
            "norms": [],
        }
        if decs:
            details[aid]["decisions"] = decs

    # norms in one pass per corpus (files are big)
    for src, prefix in (("gii", "fed_"), ("bayern_recht", "by_")):
        for n in load(src, "norms.jsonl"):
            aid = prefix + slug(n["jurabk"])
            if aid in details:
                details[aid]["norms"].append(
                    {"enbez": n["enbez"], "titel": n.get("titel") or "",
                     "text": n.get("text") or "",
                     "glied": n.get("gliederung") or ""})
    return idx, details


_BY_NEEDLES = {
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


def _by_needle(jb: str) -> str:
    return _BY_NEEDLES.get(jb, "")


# ------------------------------------------------------------- hierarchy
EU_PRIMARY_REFERENCES = [
    {
        "celex": "12016M/TXT",
        "kind": "treaty",
        "title": "Vertrag über die Europäische Union "
                 "(konsolidierte Fassung)",
        "in_force": True,
        "in_corpus": False,
    },
    {
        "celex": "12016E/TXT",
        "kind": "treaty",
        "title": "Vertrag über die Arbeitsweise der Europäischen Union "
                 "(konsolidierte Fassung)",
        "in_force": True,
        "in_corpus": False,
    },
    {
        "celex": "12016P/TXT",
        "kind": "charter",
        "title": "Charta der Grundrechte der Europäischen Union",
        "in_force": True,
        "in_corpus": False,
    },
]


def _is_constitutional_act(act: dict) -> bool:
    """Recognise the constitutional texts in the curated corpora.

    Keep both Bavarian identifiers: BAYERN.RECHT currently exposes BayVerf,
    while older/local fixtures may use the conventional abbreviation BV.
    """
    act_id = str(act.get("id") or "").casefold()
    jurabk = str(act.get("jurabk") or "").casefold()
    title = str(act.get("title") or "").casefold().strip()
    return (act_id in {"fed_gg", "by_bayverf", "by_bv"}
            or jurabk in {"gg", "bayverf", "bv"}
            or title.startswith("grundgesetz für")
            or title.startswith("verfassung des freistaates bayern"))


def _is_ordinance(act: dict) -> bool:
    """Classify the curated Rechtsverordnungen conservatively.

    Source snapshots do not currently expose a legal-form field.  Prefer the
    explicit corpus ids and only use an exact formal-title boundary as a
    forward-compatible fallback.  In particular, LStVG's "Verordnungsrecht"
    describes a statute and must not turn it into a Rechtsverordnung.
    """
    ordinance_ids = {
        "fed_aufenthv", "fed_beschv_2013", "fed_intv",
        "fed_ukraineaufenth_v", "fed_ukraineaufenthfgv",
        "by_dvasyl", "by_zustvauslr",
    }
    act_id = str(act.get("id") or "").casefold()
    title = str(act.get("title") or "").casefold().strip()
    return (act_id in ordinance_ids
            or title.startswith("verordnung ")
            or title.endswith("verordnung"))


def _legal_layers(acts: list[dict]) -> dict[str, list[dict]]:
    """Partition a flat corpus exactly once by constitutional/legal form."""
    layers: dict[str, list[dict]] = {
        "constitution": [], "statutes": [], "ordinances": [],
    }
    for act in acts:
        if _is_constitutional_act(act):
            layers["constitution"].append(act)
        elif _is_ordinance(act):
            layers["ordinances"].append(act)
        else:
            layers["statutes"].append(act)
    return layers


def build_hierarchy(wiki_idx: list[dict]) -> dict:
    vorgaenge = load("dip", "vorgaenge.jsonl")
    by_stand: dict[str, list] = {}
    for vg in vorgaenge:
        by_stand.setdefault(vg.get("beratungsstand") or "?", []).append(
            {"title": (vg.get("titel") or "")[:120],
             "date": vg.get("datum")})
    bills = load("bay_landtag", "bills.jsonl")
    by_status: dict[str, list] = {}
    for b in bills:
        by_status.setdefault(b["status"], []).append(
            {"title": b["titel"][:120], "drs": b["drs_nr"],
             "date": b.get("eingang")})
    instruments = load("eu_layer", "instruments.jsonl")
    transp = load("eu_layer", "transpositions.jsonl")
    tr_count: dict[str, int] = {}
    for t in transp:
        tr_count[t["directive_celex"]] = \
            tr_count.get(t["directive_celex"], 0) + 1
    laender: dict[str, list] = {}
    for e in load("laender_bills", "bills.jsonl") or \
             load("laender_monitor", "events.jsonl"):
        laender.setdefault(e.get("jurisdiction", "DE-?"), []).append(
            {"title": (e.get("titel") or "")[:120],
             "date": e.get("datum"),
             "url": (e.get("doc_urls") or [None])[0]})
    eu_instruments = [{
        "celex": i["celex"], "kind": i["kind"],
        "title": (i.get("title") or "")[:130],
        "in_force": i.get("in_force"),
        "geas": i.get("in_geas_core"),
        "deu_mnes": tr_count.get(i["celex"], 0),
    } for i in instruments]
    bund_acts = [a for a in wiki_idx if a["juris"] == "DE"]
    bayern_acts = [a for a in wiki_idx if a["juris"] == "DE-BY"]
    return {
        "meta": {
            "schema_version": 2,
            "model": "competence-aware",
            "not_a_total_order": True,
        },
        "eu": {
            # Keep the flat list for API clients using hierarchy schema v1.
            "instruments": eu_instruments,
            # Primary EU law is not indexed in the deep corpus.  These are
            # honest external references; secondary law below is the indexed
            # curated layer.
            "primary": {
                "indexed": False,
                "references": EU_PRIMARY_REFERENCES,
            },
            "secondary": {
                "directives": [i for i in eu_instruments
                               if i["kind"] == "directive"],
                "regulations": [i for i in eu_instruments
                                if i["kind"] == "regulation"],
                "other": [i for i in eu_instruments
                          if i["kind"] not in {"directive", "regulation"}],
            },
        },
        "bund": {
            "acts": bund_acts,
            "layers": _legal_layers(bund_acts),
            "pipeline": {k: sorted(v, key=lambda x: x["date"] or "",
                                   reverse=True)
                         for k, v in sorted(by_stand.items())},
        },
        "bayern": {
            "acts": bayern_acts,
            "layers": _legal_layers(bayern_acts),
            "pipeline": {k: v for k, v in sorted(by_status.items())},
        },
        "laender": {k: v for k, v in sorted(laender.items())},
    }


# ----------------------------------------------------------------- graph
def build_graph() -> dict:
    arena = ROOT / "data" / "lexgraph_de_wp21.qfs"
    p = parse_qfs(arena.read_bytes())
    offs = sorted(p.nodes)
    idx_of = {off: i for i, off in enumerate(offs)}

    created_by_targets = set()
    thematic_targets = set()
    for e in p.edges.values():
        if len(e["endpoints"]) == 2:
            if e["reltype"] == 10:
                created_by_targets.add(e["endpoints"][1][0])
            elif e["reltype"] == 40:
                thematic_targets.add(e["endpoints"][1][0])

    def kind_of(off: int, label: str) -> str:
        if label in HUBS:
            return "hub"
        if label.startswith("BY↯"):
            return "by-bill"
        if label.startswith("BY "):
            return "by-act"
        if label.startswith("EU "):
            return "eu"
        if label.startswith("Landtag "):
            return "land"
        if label.startswith("BGBl"):
            return "hub"
        if off in created_by_targets:
            return "initiator"
        if off in thematic_targets and " — " not in label:
            return "topic"
        if " — " in label:
            return "fed-act"
        return "vorgang"

    nodes = []
    for off in offs:
        lb = p.label(off)
        nodes.append({"label": lb[:80], "trust": p.nodes[off]["trust"],
                      "kind": kind_of(off, lb)})

    edges = [{"s": idx_of[e["endpoints"][0][0]],
              "t": idx_of[e["endpoints"][1][0]],
              "r": e["reltype"], "d": round(e["delta"], 2)}
             for e in p.edges.values() if len(e["endpoints"]) == 2
             and e["endpoints"][0][0] in idx_of
             and e["endpoints"][1][0] in idx_of]

    beliefs = [{"n": idx_of[b["subject"]], "b": b["bornTick"],
                "pT": round(b["pTrue"], 2), "pF": round(b["pFalse"], 2),
                "pN": round(b["pNone"], 2)}
               for b in p.beliefs.values() if b["subject"] in idx_of]

    born = [None] * len(nodes)
    for b in beliefs:
        if born[b["n"]] is None or b["b"] < born[b["n"]]:
            born[b["n"]] = b["b"]
    for _ in range(3):                    # propagate to belief-less nodes
        for e in edges:
            s, t = e["s"], e["t"]
            for a, b in ((s, t), (t, s)):
                if born[a] is not None and \
                        (born[b] is None or born[a] < born[b]):
                    if born[b] is None:
                        born[b] = born[a]
    ticks = sorted({s["tick"] for s in p.states.values()})
    lo = ticks[0] if ticks else 0
    for i, v in enumerate(born):
        nodes[i]["born"] = v if v is not None else lo

    return {"nodes": nodes, "edges": edges, "beliefs": beliefs,
            "ticks": ticks,
            "tick_labels": {t: month_str(t) for t in ticks},
            "worlds": [{"id": w["id"], "stability": w["stability"],
                        "contradiction": w["contradictionLevel"]}
                       for w in p.worlds.values()]}


EU_REF = re.compile(r"(Richtlinie|Verordnung)\s*\((EU|EG)\)\s*(\d{4})/(\d+)")
LANES = ["EU", "Bund", "Bayern", "Länder"]


def _hash(key: str) -> str:
    return f"{zlib.crc32(key.encode()) & 0xffffffff:08x}"


def build_git() -> dict:
    """The normative history as a git commit graph: one commit per
    legislative change, laned by jurisdiction (EU/Bund/Bayern/Länder).
    Enacted = solid commit, pending = open branch, EU-implementing bill =
    merge (a directive merged into German law). newest first."""
    raw = load("patches", "patches.jsonl")
    vorg = {str(v["id"]): v for v in load("dip", "vorgaenge.jsonl")}
    commits: list[dict] = []

    # Bund: group patch commands by procedure (a commit touches N laws)
    proc: dict[str, dict] = {}
    for p in raw:
        g = proc.setdefault(p["procedure"], {
            "title": p["procedure_title"], "status": p["status"],
            "doc": p["source_doc"], "acts": {},
            "stand": p.get("beratungsstand"),
            "decided": p.get("decided_at") or p.get("published_at")})
        g["acts"].setdefault(p["target_act"], set())
        if p["ref"].get("para"):
            g["acts"][p["target_act"]].add(p["ref"]["para"])
    for pr, g in proc.items():
        vg = vorg.get(pr.split(":")[-1], {})
        date = g["decided"] or vg.get("datum")
        if not date:
            continue
        st = g["status"]
        typ = "open" if st in ("proposed", "adopted") else "commit"
        merge_ref = None
        m = EU_REF.search(g["title"] or "")
        if m:
            kind = "L" if m.group(1) == "Richtlinie" else "R"
            merge_ref = f"3{m.group(3)}{kind}{int(m.group(4)):04d}"
            if typ == "commit":
                typ = "merge"
        paras = sorted({x for s in g["acts"].values() for x in s},
                       key=lambda v: (len(v), v))[:8]
        commits.append({
            "hash": _hash("proc:" + pr), "date": date, "lane": 1,
            "type": typ, "actor": "Bundestag", "msg": (g["title"] or "")[:280],
            "acts": list(g["acts"].keys())[:6], "paras": paras,
            "refs": [st] + ([g["stand"]] if g["stand"] else []),
            "merge_ref": merge_ref, "doc": g["doc"]})

    # Bayern: Landtag bills
    for b in load("bay_landtag", "bills.jsonl"):
        dates = [e["date"] for e in (b.get("lifecycle") or []) if e.get("date")]
        date = max(dates) if dates else b.get("eingang")
        if not date:
            continue
        st = b["status"]
        commits.append({
            "hash": _hash("by:" + str(b.get("gegenstandid"))), "date": date,
            "lane": 2, "type": "commit" if st == "verkuendet" else "open",
            "actor": "Landtag Bayern", "msg": (b.get("titel") or "")[:280],
            "acts": [], "paras": [], "refs": [st], "merge_ref": None,
            "doc": b.get("drs_nr"), "url": b.get("pdf_url"),
            "gvbl": b.get("gvbl_citation")})

    # EU: instruments (the branches everything else merges from)
    for ins in load("eu_layer", "instruments.jsonl"):
        d = ins.get("in_force_date") or ins.get("pub_date")
        if not d or d < "1990":
            continue
        commits.append({
            "hash": _hash("eu:" + ins["celex"]), "date": d, "lane": 0,
            "type": "commit", "actor": "EU",
            "msg": (ins.get("title") or "")[:280], "acts": [], "paras": [],
            "refs": [ins["celex"]] + (["GEAS"] if ins.get("in_geas_core")
                                      else []) +
                    (["außer Kraft"] if ins.get("in_force") is False else []),
            "merge_ref": None, "celex": ins["celex"]})

    # Länder: bills across all 16 states
    for b in load("laender_bills", "bills.jsonl"):
        if not b.get("datum"):
            continue
        commits.append({
            "hash": _hash("lb:" + b["event_id"]), "date": b["datum"],
            "lane": 3, "type": "open",
            "actor": "Landtag " + b["jurisdiction"].replace("DE-", ""),
            "msg": (b.get("titel") or "")[:280], "acts": [], "paras": [],
            "refs": ["Entwurf"] + (["Asyl/Sozial"] if b.get("relevant")
                                   else []),
            "merge_ref": None, "url": (b.get("doc_urls") or [None])[0]})

    commits.sort(key=lambda c: c["date"], reverse=True)
    return {"lanes": LANES, "commits": commits, "total": len(commits)}


def main() -> int:
    (WEB / "acts").mkdir(parents=True, exist_ok=True)
    feed = build_feed()
    wiki_idx, details = build_wiki()
    hierarchy = build_hierarchy(wiki_idx)
    graph = build_graph()
    git = build_git()
    decisions = load_decisions()
    eu_index = build_eu_index()
    search_counts = build_search_database(
        details, WEB / "search.sqlite", ROOT / "data" / "search_synonyms.json")

    patches = load("patches", "patches.jsonl")
    sts: dict[str, int] = {}
    for p_ in patches:
        sts[p_["status"]] = sts.get(p_["status"], 0) + 1
    bills = load("bay_landtag", "bills.jsonl")
    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "acts_fed": sum(1 for a in wiki_idx if a["juris"] == "DE"),
        "acts_by": sum(1 for a in wiki_idx if a["juris"] == "DE-BY"),
        "patches": sts,
        "vorgaenge": len(load("dip", "vorgaenge.jsonl")),
        "bay_bills": len(bills),
        "bay_verkuendet": sum(1 for b in bills
                              if b["status"] == "verkuendet"),
        "eu_instruments": len(load("eu_layer", "instruments.jsonl")),
        "eu_index_total": eu_index["total"] if eu_index else 0,
        "transpositions": len(load("eu_layer", "transpositions.jsonl")),
        "feed_events": len(feed),
        "decisions": len(decisions),
        "search": search_counts,
        "graph": {k: len(v) for k, v in graph.items()
                  if isinstance(v, list)},
    }

    def dump(name: str, obj) -> int:
        f = WEB / name
        f.write_text(json.dumps(obj, ensure_ascii=False,
                                separators=(",", ":")))
        return f.stat().st_size

    sizes = {
        "summary.json": dump("summary.json", summary),
        "feed.json": dump("feed.json", feed),
        "wiki.json": dump("wiki.json", wiki_idx),
        "decisions.json": dump("decisions.json", decisions),
        "hierarchy.json": dump("hierarchy.json", hierarchy),
        "graph.json": dump("graph.json", graph),
        "git.json": dump("git.json", git),
    }
    if eu_index:
        sizes["eu_index.json"] = dump("eu_index.json", eu_index)
    else:
        (WEB / "eu_index.json").unlink(missing_ok=True)
    for aid, d in details.items():
        dump(f"acts/{aid}.json", d)
    print(f"web data -> {WEB}")
    for k, v in sizes.items():
        print(f"  {k:16} {v/1024:8.1f} KB")
    print(f"  acts/*.json      {len(details)} files")
    print(f"  search.sqlite    {search_counts['acts']} acts / "
          f"{search_counts['norms']} norms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
