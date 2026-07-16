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
    web/data/gii_catalog.json  every official GII act as metadata only
    web/data/watched_procedures.json  persistent DIP/EUR-Lex watch state
    web/data/amendment_fates.json     reviewed document-chain validations
    web/data/verified_federal_events.json  official-only federal history
    web/data/official_federal_states.json  cumulative GII observation manifest
    web/data/official_transition_reviews.json  BGBl/DIP accepted legal dates
    web/data/federal_states/…   immutable complete GII state objects
    web/data/retrospective_history.json  legal-time + knowledge-time history
    web/data/retrospective_history.sqlite  portable relational history
    web/data/citations.json   citation metadata/counts (no row payload)
    web/data/citations.sqlite exact current statutory citations + backlinks
    web/data/search.sqlite     ranked full-text index (acts + current norms)
    web/data/hierarchy.json    competence-aware legal layers (no geometry)
    web/data/graph.json        arena export: nodes/edges/beliefs/ticks/worlds
"""
from __future__ import annotations

import html
import hashlib
import json
import os
import re
import shutil
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
from procedure_analysis import analyse_procedure           # noqa: E402
from federal_history import (                             # noqa: E402
    build_public_federal_history,
    official_state_transition_events,
)
from official_states import (                             # noqa: E402
    DEFAULT_STORE as FEDERAL_STATE_STORE,
    load_manifest as load_official_state_manifest,
    load_state_verified as load_official_state,
    store_state_object,
    transitions as build_official_state_transitions,
)
from official_transition_review import review_transitions  # noqa: E402
from retrospective_history import (                       # noqa: E402
    build_public_manifest,
    materialize_history,
    write_sqlite as write_retrospective_sqlite,
)
from verified_reconstruction import (                     # noqa: E402
    build_from_paths as build_verified_reconstructions,
)
from tools.statutory_citations import (                    # noqa: E402
    build_citation_index,
    citation_manifest,
    write_citation_database,
)

WEB = ROOT / "web" / "data"

HUBS = {"WIRD_GESETZ", "BGBl (verkündet)", "Gesetzgebungsverfahren",
        "GVBl/BayMBl (Bayern)", "TEXTÄNDERUNG (BayRS)",
        "TEXTÄNDERUNG in Kraft", "KONSOLIDIERT (NeuRIS)",
        "Amtsblatt der EU (OJ L)", "Asyl/Migration (Länder-Monitor)"}

# Always expose all 16 Länder, including an explicit empty array when the
# upstream six-month window has a known ingestion gap.  Absence of a key used
# to make Bremen disappear entirely and overstated the monitor's coverage.
ALL_LAENDER_JURISDICTIONS = (
    "DE-BB", "DE-BE", "DE-BW", "DE-BY", "DE-HB", "DE-HE", "DE-HH",
    "DE-MV", "DE-NI", "DE-NW", "DE-RP", "DE-SH", "DE-SL", "DE-SN",
    "DE-ST", "DE-TH",
)


# Cumulative word-diff ledger for Bavarian law.  BAYERN.RECHT serves only the
# current consolidation, so current changes come from adjacent daily snapshots;
# a conservative Wayback pass supplies sparse historical transitions where two
# archived states can be tied unambiguously to one official FFN event.
BY_DIFF_LEDGER = ROOT / "data" / "by_diffs.jsonl"
PROCEDURE_WATCHLIST = ROOT / "data" / "procedure_watchlist.json"
PROCEDURE_WATCH_STATE = ROOT / "data" / "procedure_watch_state.json"
PROCEDURE_WATCH_HISTORY = ROOT / "data" / "procedure_watch_history.jsonl"
AMENDMENT_FATES = ROOT / "data" / "amendment_fates.json"
BUZER_CROSS_CHECKS = ROOT / "data" / "buzer_cross_checks.json"

# These snapshots may be retained locally for research/verification, but they
# are not part of the public data plane without separate database-reuse
# permission.  Statutory text being an official work does not extinguish a
# private database producer's rights in systematic selection/arrangement.
QUARANTINED_SOURCES = frozenset({
    "buzer", "buzer_synopse", "laender_bills", "laender_monitor",
})


def include_quarantined_sources() -> bool:
    """Explicit private/research opt-in; public builds must leave this off."""
    return os.environ.get("LEXGRAPH_INCLUDE_QUARANTINED") == "1"


def load_buzer_cross_checks() -> dict[str, int]:
    """Small, curated deep-link list; never loads Buzer's private snapshots."""
    if not BUZER_CROSS_CHECKS.is_file():
        return {}
    payload = _read_json_object(BUZER_CROSS_CHECKS, {})
    rows = payload.get("acts") or {}
    return {str(jurabk): int(act_id) for jurabk, act_id in rows.items()
            if str(act_id).isdigit() and int(act_id) > 0}


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
        added.append({"jurabk": jb, "date": days[-1],
                      "effective_date": days[-1], "para": enbez,
                      "old": o, "new": n,
                      "source": "daily_snapshot"})
    if added:
        with BY_DIFF_LEDGER.open("a", encoding="utf-8") as f:
            for r in added:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  by-diffs: +{len(added)} changed norms "
              f"({days[-2]} -> {days[-1]})")


def load_by_diffs() -> dict[str, dict[str, list[dict]]]:
    """Load Bavarian word diffs without losing their validity date.

    ``date`` is the promulgation/event date used to attach a diff to the
    visible version timeline.  The legal state changes on ``effective_date``;
    archive reconstruction must never silently substitute the former for the
    latter.  Older daily-snapshot rows predate that field, so their snapshot
    date is the only honest fallback and their provenance remains visible.
    """
    _update_by_diff_ledger()
    out: dict[str, dict[str, list[dict]]] = {}
    if BY_DIFF_LEDGER.is_file():
        for r in read_jsonl(BY_DIFF_LEDGER):
            change = {
                "para": r["para"],
                "old": r["old"],
                "new": r["new"],
                "effective_date": r.get("effective_date") or r["date"],
                "source": r.get("source") or "unknown",
            }
            for key in ("transition_id", "confidence", "operation",
                        "event_source", "event_id", "event_seq",
                        "event_description", "old_valid", "new_valid",
                        "old_capture", "new_capture"):
                if r.get(key) is not None:
                    change[key] = r[key]
            out.setdefault(r["jurabk"], {}).setdefault(r["date"], []).append(
                change)
    return out


def build_eu_index() -> dict | None:
    """Breadth layer: every in-force EU directive + basic regulation as
    metadata rows (fetch_eu_index.py snapshot). None when never fetched."""
    snap = latest_snapshot("eu_index")
    if not snap or not (snap / "instruments.jsonl").is_file():
        return None
    rows = list(read_jsonl(snap / "instruments.jsonl"))
    return {"built_at": snap.name, "total": len(rows), "instruments": rows}


def build_gii_catalog(wiki_idx: list[dict]) -> dict | None:
    """Export the complete official GII TOC as a metadata-only breadth layer.

    Only acts already in the curated corpus receive an ``act_id`` and parsed
    ``jurabk``.  No norm or full-text fetch is implied for the other entries.
    """
    snap = latest_snapshot("gii")
    if not snap or not (snap / "catalog.jsonl").is_file():
        return None

    wiki_by_jurabk = {
        str(row.get("jurabk")): row
        for row in wiki_idx if row.get("juris") == "DE"
    }
    curated_by_slug = {
        str(row.get("slug")): wiki_by_jurabk.get(str(row.get("jurabk")))
        for row in read_jsonl(snap / "acts.jsonl")
    } if (snap / "acts.jsonl").is_file() else {}

    rows: list[dict] = []
    for source in read_jsonl(snap / "catalog.jsonl"):
        row = {key: source.get(key) for key in (
            "id", "abbrev", "title", "url")}
        curated = curated_by_slug.get(str(source.get("abbrev") or ""))
        if curated:
            row["act_id"] = curated["id"]
            row["jurabk"] = curated["jurabk"]
        rows.append(row)
    return {"schema_version": 1, "built_at": snap.name,
            "total": len(rows), "acts": rows}


def load(source: str, name: str) -> list[dict]:
    if source in QUARANTINED_SOURCES and not include_quarantined_sources():
        return []
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
    # A PatchInstruction comes from a draft and carries a bill-level date.
    # Even after the procedure reaches Verkündet, that is not a verified
    # per-command effective date. Only explicitly verified dates drive this
    # act-level clock.
    past_dates = {v["date"] for v in versions if v.get("date")}
    past_dates.update(
        p["valid_from"] for p in patches
        if p.get("status") == "published" and p.get("valid_from")
        and p.get("valid_from_verified") is True
        and p["valid_from"] <= TODAY)
    past = sorted(past_dates, reverse=True)
    fut = [u["date"] for u in upcoming
           if u.get("date") and u["date"] > TODAY]
    fut += [p["valid_from"] for p in patches
            if p.get("valid_from")
            and p.get("valid_from_verified") is True
            and p["valid_from"] > TODAY]
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
    buzer_links = load_buzer_cross_checks()
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
            "valid_from_verified": False,
            "valid_from_basis": "draft_bill_clause_unverified_for_patch",
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
        if buzer_act_id := buzer_links.get(jb):
            details[aid]["cross_checks"] = [{
                "source": "buzer",
                "label": "Buzer",
                "coverage": "history_since_2006",
                "authoritative": False,
                "url": (f"https://www.buzer.de/gesetz/{buzer_act_id}/"
                        "l.htm"),
            }]
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


def _plain_dip_text(value: object) -> str:
    """Turn the small HTML fragments returned by DIP into searchable text."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _procedure_watchlist() -> dict[str, dict]:
    if not PROCEDURE_WATCHLIST.is_file():
        return {}
    payload = json.loads(PROCEDURE_WATCHLIST.read_text(encoding="utf-8"))
    procedures = payload.get("procedures") or {}
    return {str(key): value for key, value in procedures.items()
            if isinstance(value, dict)}


def _procedure_row(vg: dict, watchlist: dict[str, dict]) -> dict:
    procedure_id = str(vg.get("id") or "")
    watch = watchlist.get(procedure_id)
    descriptors = sorted({str(item.get("name"))
                          for item in vg.get("deskriptor") or []
                          if isinstance(item, dict) and item.get("name")})
    row = {
        "id": procedure_id,
        "source": "DIP",
        "jurisdiction": "DE",
        "procedure": procedure_id,
        "title": str(vg.get("titel") or ""),
        "date": vg.get("datum"),
        "updated": vg.get("aktualisiert"),
        "status": vg.get("beratungsstand") or "?",
        "gesta": vg.get("gesta"),
        "topics": list(vg.get("sachgebiet") or []),
        "initiators": list(vg.get("initiative") or []),
        "descriptors": descriptors,
        "summary": _plain_dip_text(vg.get("abstract")),
        "url": (f"https://dip.bundestag.de/vorgang/_/{procedure_id}"
                if procedure_id else None),
        "watched": watch is not None,
    }
    if watch is not None:
        row["watch"] = watch
    return row


def _eu_procedure_row(source: dict,
                      watchlist: dict[str, dict]) -> dict:
    """Project an official EUR-Lex watch snapshot into hierarchy/search.

    Unlike the broad EU instrument index, these rows are pending procedures.
    They therefore retain their official stage and never imply that a proposal
    is already law.
    """
    procedure_id = str(source.get("id") or source.get("procedure") or "")
    watch = watchlist.get(procedure_id)
    row = {
        "id": procedure_id,
        "source": "EUR-Lex",
        "jurisdiction": "EU",
        "procedure": source.get("procedure"),
        "proposal_celex": source.get("proposal_celex"),
        "title": str(source.get("title") or ""),
        "date": source.get("date"),
        "updated": source.get("updated") or source.get("fetched_at"),
        "status": source.get("status") or "?",
        "stage": source.get("stage") or source.get("status") or "?",
        "gesta": None,
        "topics": [],
        "initiators": ([str(watch.get("initiated_by"))]
                       if watch and watch.get("initiated_by") else []),
        "descriptors": [],
        "summary": str((watch or {}).get("scope") or ""),
        "events": source.get("events") or [],
        "council_development": source.get("council_development"),
        "adopted_celexes": source.get("adopted_celexes") or [],
        "official_journal": source.get("official_journal") or [],
        "publication_detected": bool(source.get("publication_detected")),
        "awaiting_final_review": bool(source.get("awaiting_final_review")),
        "final_text_review": source.get("final_text_review"),
        "terminal": bool(source.get("terminal")),
        "url": source.get("url") or (watch or {}).get("official_url"),
        "watched": watch is not None,
    }
    if watch is not None:
        row["watch"] = watch
    return row


def _pipeline_rows(hierarchy: dict) -> list[dict]:
    """Flatten the two official procedure lanes without schema assumptions."""
    rows: list[dict] = []
    for jurisdiction in ("bund", "eu"):
        pipeline = (hierarchy.get(jurisdiction) or {}).get("pipeline") or {}
        groups = pipeline.values() if isinstance(pipeline, dict) else [pipeline]
        rows.extend(row for group in groups if isinstance(group, list)
                    for row in group if isinstance(row, dict))
    return rows


def _read_json_object(path: Path, fallback: dict) -> dict:
    if not path.is_file():
        return fallback
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return value if isinstance(value, dict) else fallback


def build_watched_procedures(hierarchy: dict,
                             amendment_fates: dict | None = None,
                             analysed_at: str | None = None) -> dict:
    """Merge persistent observations, configuration and change history.

    ``update_procedure_watch.py`` owns state transitions.  This exporter only
    presents that ledger.  A fallback from the latest hierarchy snapshot keeps
    local builds usable before the updater has run for the first time.
    """
    configs = _procedure_watchlist()
    state = _read_json_object(
        PROCEDURE_WATCH_STATE, {"schema_version": 1, "procedures": {}})
    state_rows = state.get("procedures") or {}
    official = {str(row.get("id") or ""): row
                for row in _pipeline_rows(hierarchy) if row.get("id")}
    history_rows = (list(read_jsonl(PROCEDURE_WATCH_HISTORY))
                    if PROCEDURE_WATCH_HISTORY.is_file() else [])
    history: dict[str, list[dict]] = {}
    for event in history_rows:
        history.setdefault(str(event.get("id") or ""), []).append(event)

    terminal_statuses = {
        "Verkündet", "Abgelehnt", "Für erledigt erklärt",
        "Einbringung abgelehnt",
    }
    procedures: list[dict] = []
    for key, config in configs.items():
        observed = state_rows.get(key)
        if not isinstance(observed, dict):
            source = official.get(key) or {}
            terminal = (bool(source.get("terminal")) or
                        source.get("status") in terminal_statuses)
            monitor = bool(config.get("monitor", True))
            active = monitor and not terminal
            awaiting_review = bool(source.get("awaiting_final_review"))
            observed = {
                "id": key,
                "watch_id": config.get("id") or key,
                "source": config.get("source") or source.get("source") or "DIP",
                "jurisdiction": config.get("jurisdiction") or
                                source.get("jurisdiction"),
                "procedure": source.get("procedure") or
                             config.get("procedure") or key,
                "gesta": source.get("gesta"),
                "title": source.get("title") or
                         config.get("procedure") or key,
                "status": source.get("status") or
                          "Not found in latest official snapshot",
                "stage": source.get("stage") or source.get("status") or
                         "source_missing",
                "date": source.get("date"),
                "updated": source.get("updated"),
                "url": source.get("url") or config.get("official_url"),
                "terminal": terminal,
                "active": active,
                "publication_detected": bool(source.get("publication_detected")),
                "awaiting_final_review": awaiting_review,
                "final_text_review": source.get("final_text_review"),
                "council_development": source.get("council_development"),
                "tracking_state": ("pending_final_review"
                                   if active and awaiting_review else
                                   "active" if active else
                                   "terminal" if terminal else "archived"),
            }
        row = dict(observed)
        # Reviewed watch metadata is deliberately explicit at the API layer:
        # callers should not need to reverse-engineer aliases or scope from
        # search results.
        row.update({
            "queries": list(config.get("queries") or []),
            "scope": config.get("scope"),
            "scope_source": config.get("scope_source"),
            "draft_only": bool(config.get("draft_only", False)),
            "cutoff": config.get("cutoff"),
            "entry_date_is_criterion": config.get("entry_date_is_criterion"),
            "initiated_by": config.get("initiated_by"),
            "decided_by": config.get("decided_by"),
            "proposal_url": config.get("proposal_url"),
            "council_documents": list(config.get("council_documents") or []),
            "council_register_document": config.get(
                "council_register_document"),
            "council_register_url": config.get("council_register_url"),
            "relevant_norms": list(config.get("relevant_norms") or []),
            "validation_ids": list(config.get("validation_ids") or []),
            "terminal_rule": config.get("terminal_rule"),
            "official_url": config.get("official_url") or row.get("url"),
            "history": sorted(history.get(key, []),
                              key=lambda event: event.get("observed_at") or ""),
        })
        row["analysis"] = analyse_procedure(
            row, config, row["history"],
            list((amendment_fates or {}).get("records") or []),
            analysed_at or state.get("checked_at") or row.get("last_checked"))
        procedures.append(row)

    procedures.sort(key=lambda row: str(row.get("id") or ""))
    procedures.sort(key=lambda row: str(row.get("date") or ""),
                    reverse=True)
    procedures.sort(key=lambda row: (0 if row.get("active") else
                                     1 if row.get("terminal") else 2))
    return {
        "schema_version": 2,
        "checked_at": state.get("checked_at"),
        "active_count": sum(bool(row.get("active")) for row in procedures),
        "terminal_count": sum(bool(row.get("terminal")) for row in procedures),
        "archived_count": sum(not row.get("active") and
                              not row.get("terminal") for row in procedures),
        "procedures": procedures,
    }


def build_hierarchy(wiki_idx: list[dict]) -> dict:
    vorgaenge = load("dip", "vorgaenge.jsonl")
    watchlist = _procedure_watchlist()
    by_stand: dict[str, list] = {}
    for vg in vorgaenge:
        status = vg.get("beratungsstand") or "?"
        by_stand.setdefault(status, []).append(
            _procedure_row(vg, watchlist))
    eu_by_status: dict[str, list] = {}
    for source in load("eu_watch", "procedures.jsonl"):
        row = _eu_procedure_row(source, watchlist)
        status = str(row.get("status") or "?")
        eu_by_status.setdefault(status, []).append(row)
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
    laender: dict[str, list] = {
        jurisdiction: [] for jurisdiction in ALL_LAENDER_JURISDICTIONS}
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
            "coverage": {
                "laender_monitor": (
                    "research_only" if include_quarantined_sources()
                    else "origin_verification_required"),
                "laender_keys": len(ALL_LAENDER_JURISDICTIONS),
            },
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
            # Explicitly watched pending EU procedures.  These remain
            # separate from instruments in force and preserve the official
            # EUR-Lex stage verbatim.
            "pipeline": {
                key: sorted(rows, key=lambda row: row.get("date") or "",
                            reverse=True)
                for key, rows in sorted(eu_by_status.items())
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


# ------------------------------------------------------- amendment fates
def _norm_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _check_current_law(details: dict[str, dict], check: dict) -> dict:
    """Evaluate one deliberately small, auditable current-law assertion."""
    act_id = str(check.get("act_id") or "")
    designator = _norm_key(check.get("norm"))
    act = details.get(act_id)
    result = dict(check)
    if not act:
        result.update({"passed": False, "reason": "act_not_in_corpus"})
        return result
    norm = next((row for row in act.get("norms") or []
                 if _norm_key(row.get("enbez")) == designator), None)
    check_type = check.get("type")
    if check_type == "norm_absent":
        result.update({
            "passed": norm is None,
            "reason": "norm_absent" if norm is None else "norm_present",
            "observed_norm": norm.get("enbez") if norm else None,
        })
        return result
    if check_type == "norm_text_contains":
        if norm is None:
            result.update({"passed": False, "reason": "norm_not_found"})
            return result
        needle = _norm_key(check.get("text"))
        text = " ".join(str(norm.get("text") or "").split())
        folded = text.casefold()
        passed = bool(needle) and needle in folded
        excerpt = None
        if passed:
            start = max(0, folded.index(needle) - 90)
            end = min(len(text), folded.index(needle) + len(needle) + 140)
            excerpt = text[start:end]
        result.update({
            "passed": passed,
            "reason": "text_found" if passed else "text_not_found",
            "observed_norm": norm.get("enbez"),
            "evidence_excerpt": excerpt,
        })
        return result
    result.update({"passed": False, "reason": "unknown_check_type"})
    return result


def build_amendment_fates(details: dict[str, dict],
                          checked_at: str | None = None) -> dict:
    """Publish reviewed document chains plus mechanical current-law checks.

    The exporter does *not* pretend to derive the parliamentary document
    chain from current statutory text.  Those roles are curated with official
    links; only the declared ``current_law_checks`` are machine-evaluated.
    """
    source = _read_json_object(
        AMENDMENT_FATES, {"schema_version": 1, "records": []})
    timestamp = checked_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    records: list[dict] = []
    for original in source.get("records") or []:
        if not isinstance(original, dict):
            continue
        row = dict(original)
        checks = [_check_current_law(details, check)
                  for check in original.get("current_law_checks") or []
                  if isinstance(check, dict)]
        row["validation"] = {
            "checked_at": timestamp,
            "method": "current Lexgraph corpus checks",
            "passed": bool(checks) and all(check.get("passed")
                                           for check in checks),
            "checks": checks,
        }
        records.append(row)
    return {
        "schema_version": int(source.get("schema_version") or 1),
        "built_at": timestamp,
        "total": len(records),
        "validated": sum(bool((row.get("validation") or {}).get("passed"))
                         for row in records),
        "records": records,
    }


# ----------------------------------------------------------------- graph
def build_graph() -> dict:
    arena = ROOT / "data" / "lexgraph_de_wp21.qfs"
    policy_path = Path(f"{arena}.policy.json")
    if not policy_path.is_file():
        raise RuntimeError(
            "QFS provenance policy is missing; run tools/build_qfs.py before "
            "publishing web data")
    policy = _read_json_object(policy_path, {})
    arena_bytes = arena.read_bytes()
    digest = hashlib.sha256(arena_bytes).hexdigest()
    if policy.get("qfs_sha256") != digest:
        raise RuntimeError(
            "QFS provenance policy does not match the arena; rebuild QFS")
    expected_quarantine = include_quarantined_sources()
    if bool(policy.get("includes_quarantined_sources")) != expected_quarantine:
        mode = "private research" if expected_quarantine else "public"
        raise RuntimeError(
            f"QFS provenance is incompatible with the requested {mode} "
            "web build; rebuild QFS in the same source-policy mode")
    if not expected_quarantine and not policy.get("public_build"):
        raise RuntimeError("refusing to publish a non-public QFS arena")

    p = parse_qfs(arena_bytes)
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
                       for w in p.worlds.values()],
            "source_policy": {
                "public_build": bool(policy.get("public_build")),
                "includes_quarantined_sources": bool(
                    policy.get("includes_quarantined_sources")),
                "built_at": policy.get("built_at"),
                "qfs_sha256": digest,
            }}


EU_REF = re.compile(r"(Richtlinie|Verordnung)\s*\((EU|EG)\)\s*(\d{4})/(\d+)")
LANES = ["EU", "Bund", "Bayern", "Länder"]


def _hash(key: str) -> str:
    return f"{zlib.crc32(key.encode()) & 0xffffffff:08x}"


def build_git(official_transitions: list[dict] | None = None,
              transition_reviews: list[dict] | None = None,
              historical_events: list[dict] | None = None) -> dict:
    """The normative history as a git commit graph: one commit per
    legislative change, laned by jurisdiction (EU/Bund/Bayern/Länder).
    Promulgated procedure = solid commit, pending = open branch,
    rejected/not-merged = closed branch, and a legally specific EU relation =
    merge. DIP target norms remain labelled as draft-derived. Newest first."""
    raw = load("patches", "patches.jsonl")
    vorg = {str(v["id"]): v for v in load("dip", "vorgaenge.jsonl")}
    commits: list[dict] = []

    # Official GII observations are real Git-like state commits, but their
    # retrieval date is deliberately not presented as Inkrafttreten.  The
    # checkout pointer addresses the complete immutable state object.
    for transition in official_transitions or []:
        paras = []
        for change in transition.get("changes") or []:
            label = str(change.get("para") or "").strip()
            label = re.sub(r"^(?:§+|Art(?:ikel)?\.?)\s*", "", label,
                           flags=re.IGNORECASE)
            if label:
                paras.append(label)
        digest = str(transition.get("state_sha256") or "")
        commits.append({
            "hash": _hash("gii-state:" + digest),
            "date": transition["observed_at"],
            "lane": 1,
            "type": "commit",
            "actor": "GII",
            "msg": "Amtlicher konsolidierter Stand beobachtet",
            "acts": [transition["jurabk"]],
            "paras": paras[:20],
            "refs": ["official_state_observed", "not_effective_date"],
            "merge_ref": None,
            "url": transition.get("source_url"),
            "targets_verified": True,
            "target_basis": "complete_content_addressed_gii_state_pair",
            "date_basis": "retrieval_observation_not_effective_date",
            "verification": "exact",
            "observed_at": transition["observed_at"],
            "state_digest": digest,
            "previous_state_digest": transition.get(
                "previous_state_sha256"),
            "checkout": {
                "act_id": transition["act_id"],
                "observed_at": transition["observed_at"],
                "state_digest": digest,
            },
        })

    # A legal-effect commit is separate from the later GII observation.  It is
    # emitted only after a final, integrity-checked BGBl command was matched to
    # the complete state pair and DIP supplied the article-specific
    # commencement date.  This keeps the graph useful without pretending that
    # the crawler observed the law on the day it entered into force.
    for review in transition_reviews or []:
        paras = []
        for change in review.get("changes") or []:
            label = str(change.get("para") or "").strip()
            label = re.sub(r"^(?:§+|Art(?:ikel)?\.?)\s*", "", label,
                           flags=re.IGNORECASE)
            if label:
                paras.append(label)
        bgbl = dict(review.get("bgbl") or {})
        articles = [str(value) for value in
                    (review.get("amending_articles") or []) if value]
        refs = ["effective_date_verified"]
        if bgbl.get("document_id"):
            refs.append(str(bgbl["document_id"]))
        refs.extend(f"Artikel {value}" for value in articles)
        commits.append({
            "hash": _hash("official-review:" + str(review.get("id") or "")),
            "date": review["effective_at"],
            "lane": 1,
            "type": "commit",
            "actor": "BGBl / Lexgraph",
            "msg": ("Amtliche Änderung wirksam · "
                    f"{review.get('jurabk') or review.get('act') or ''}"),
            "acts": [review.get("jurabk") or review.get("act")],
            "paras": paras[:20],
            "refs": refs,
            "merge_ref": None,
            "url": bgbl.get("pdf_url"),
            "targets_verified": True,
            "target_basis": (
                "final_bgbl_command+complete_gii_state_pair+"
                "dip_article_commencement"),
            "date_basis": review.get("date_basis"),
            "verification": review.get("verification"),
            "legal_verification": review.get("verification"),
            "legal_effect_verified": True,
            "published_at": review.get("published_at"),
            "effective_at": review.get("effective_at"),
            "observed_at": review.get("observed_at"),
            "state_digest": review.get("state_sha256"),
            "previous_state_digest": review.get("previous_state_sha256"),
            "review_id": review.get("id"),
            "procedure_id": review.get("procedure_id"),
            "bgbl": bgbl,
        })

    # 2023+ final BGBl inventory.  These are verified promulgation/amendment
    # events, not reconstructed historical bodies.  An article-wide DIP
    # commencement date is used when it is explicit; otherwise the graph keeps
    # the publication date and labels the legal-effect date unresolved.
    reviewed_documents = {
        (str(row.get("act_id") or ""),
         str((row.get("bgbl") or {}).get("document_id") or ""))
        for row in transition_reviews or []
    }
    for event in historical_events or []:
        act_id = str(event.get("act_id") or "")
        document_id = str(event.get("document_id") or "")
        if not act_id or not document_id or \
                (act_id, document_id) in reviewed_documents:
            continue
        effective = event.get("effective_at")
        published = event.get("publication_date")
        event_date = effective or published
        if not event_date:
            continue
        jurabk = str(event.get("jurabk") or "")
        article = str(event.get("amending_article") or "")
        refs = [document_id]
        if article:
            refs.append(f"Artikel {article}")
        if effective:
            refs.append("effective_date_verified")
        else:
            refs.append("effective_date_unresolved")
        commits.append({
            "hash": _hash("bgbl-history:" + str(event.get("id") or "")),
            "date": event_date,
            "lane": 1,
            "type": "commit",
            "actor": "BGBl / Lexgraph",
            "msg": (("Amtliche Änderung wirksam" if effective else
                     "Amtliche Änderung veröffentlicht") +
                    (f" · {jurabk}" if jurabk else "")),
            "acts": [jurabk] if jurabk else [],
            "paras": list(event.get("affected_norms") or [])[:20],
            "refs": refs,
            "merge_ref": None,
            "url": event.get("official_pdf_url"),
            "targets_verified": True,
            "target_basis": event.get("match_basis"),
            "date_basis": ("official_dip_article_commencement_clause"
                           if effective else "official_bgbl_publication_date"),
            "verification": event.get("match_basis"),
            "legal_effect_verified": bool(effective),
            "candidate_only": True,
            "historical_text_reconstructed": False,
            "published_at": published,
            "effective_at": effective,
            "event_id": event.get("id"),
            "procedure_id": event.get("procedure_id"),
            "bgbl": {
                "document_id": document_id,
                "pdf_url": event.get("official_pdf_url"),
                "pdf_sha256": event.get("pdf_sha256"),
                "integrity_verified": event.get("integrity_verified") is True,
            },
        })

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
        typ = ("open" if st in ("proposed", "adopted")
               else "commit" if st == "published" else "closed")
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
            "merge_ref": merge_ref, "doc": g["doc"],
            "targets_verified": False,
            "target_basis": "draft_patch_instructions"})

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


def publish_official_federal_states() -> tuple[dict, list[dict]]:
    """Verify the durable GII CAS and copy its referenced objects to web.

    The source store is cumulative; the public manifest is authoritative, so
    stale unreferenced content-addressed files are harmless.  Existing blobs
    are reused byte-for-byte and each referenced canonical state is verified
    before publication.
    """
    manifest = load_official_state_manifest(FEDERAL_STATE_STORE)
    observations = list(manifest.get("observations") or [])
    objects = manifest.get("objects") or {}
    destination = WEB / "federal_states"
    for digest, metadata in objects.items():
        load_official_state(FEDERAL_STATE_STORE, digest)
        relative = Path(str(metadata["path"]))
        source = FEDERAL_STATE_STORE / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)
    change_rows = build_official_state_transitions(
        manifest, FEDERAL_STATE_STORE) if observations else []
    public = {
        **manifest,
        "total_observations": len(observations),
        "total_states": len(objects),
        "total_transitions": len(change_rows),
        "archive_start": min(
            (row["observed_at"] for row in observations), default=None),
        "archive_end": max(
            (row["observed_at"] for row in observations), default=None),
        "source_policy": {
            "official_only": True,
            "source": "GII",
            "date_basis": "retrieval_observation_not_effective_date",
            "effective_dates_inferred": False,
        },
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.json").write_text(
        json.dumps(public, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    return public, change_rows


def _join_transition_reviews(transitions: list[dict],
                             reviews: list[dict]) -> list[dict]:
    """Attach legal provenance without weakening observation semantics.

    ``transitions`` remains the immutable retrieval ledger used for checkout.
    This derived copy is used for legal history events, where one independently
    reviewed BGBl/DIP match may add publication/effect dates.  Duplicate
    reviews fail closed because choosing one would manufacture certainty.
    """
    indexed: dict[tuple[str, str, str], dict] = {}
    for review in reviews:
        key = (str(review.get("act_id") or ""),
               str(review.get("previous_state_sha256") or ""),
               str(review.get("state_sha256") or ""))
        if key in indexed:
            raise ValueError(f"ambiguous official transition review: {key}")
        indexed[key] = review

    out = []
    for transition in transitions:
        key = (str(transition.get("act_id") or ""),
               str(transition.get("previous_state_sha256") or ""),
               str(transition.get("state_sha256") or ""))
        review = indexed.get(key)
        row = dict(transition)
        if review:
            row.update({
                "published_at": review["published_at"],
                "effective_at": review["effective_at"],
                "date_basis": review["date_basis"],
                "legal_effect_verified": True,
                "legal_verification": review["verification"],
                "review_id": review["id"],
                "review_evidence": list(review.get("evidence") or []),
                "amending_articles": list(
                    review.get("amending_articles") or []),
                "procedure_id": review.get("procedure_id"),
                "bgbl": dict(review.get("bgbl") or {}),
            })
        out.append(row)
    return out


def main() -> int:
    (WEB / "acts").mkdir(parents=True, exist_ok=True)
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    official_states, official_transitions = publish_official_federal_states()
    bgbl_documents = load("bgbl_documents", "documents.jsonl")
    transition_reviews = review_transitions(
        official_transitions, bgbl_documents)
    reviewed_transitions = _join_transition_reviews(
        official_transitions, transition_reviews)
    official_state_objects = {
        digest: load_official_state(FEDERAL_STATE_STORE, digest)
        for digest in sorted(official_states.get("objects") or {})
    }
    internal_retrospective = materialize_history(
        official_states.get("observations") or [],
        official_state_objects,
        transition_reviews,
    )
    retrospective_candidates = load(
        "bgbl_history_backfill", "candidates.jsonl")
    verified_reconstructions = build_verified_reconstructions(
        built_at=built_at)
    derived_states = verified_reconstructions.get("state_objects") or {}
    derived_metadata = {}
    for digest, state in sorted(derived_states.items()):
        stored_digest, metadata = store_state_object(
            WEB / "federal_states", state)
        if stored_digest != digest:
            raise RuntimeError(
                f"verified reconstruction digest changed: {digest}")
        reviewed_metadata = dict(
            (verified_reconstructions.get("object_metadata") or {})[digest])
        derived_metadata[digest] = {**metadata, **reviewed_metadata}
    verified_reconstructions["object_metadata"] = derived_metadata
    all_retrospective_states = {
        **official_state_objects,
        **derived_states,
    }
    previous_retrospective = None
    previous_path = WEB / "retrospective_history.json"
    if previous_path.is_file():
        try:
            candidate = json.loads(previous_path.read_text(encoding="utf-8"))
            if candidate.get("kind") == "lexgraph-retrospective-history":
                previous_retrospective = candidate
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            previous_retrospective = None
    retrospective = build_public_manifest(
        internal_retrospective,
        all_retrospective_states,
        official_states.get("objects") or {},
        retrospective_candidates,
        built_at=built_at,
        previous=previous_retrospective,
        verified_reconstructions=verified_reconstructions,
    )
    public_reconstructions = {
        key: value for key, value in verified_reconstructions.items()
        if key != "state_objects"
    }
    feed = build_feed()
    wiki_idx, details = build_wiki()
    citation_snapshots = {}
    for juris, family in (("DE", "gii"), ("DE-BY", "bayern_recht")):
        snapshot = latest_snapshot(family)
        if snapshot:
            citation_snapshots[juris] = snapshot.name
    citation_index = build_citation_index(
        details.values(), built_at=built_at,
        source_snapshots=citation_snapshots)
    write_citation_database(WEB / "citations.sqlite", citation_index)
    citations = citation_manifest(citation_index)
    # The manifest is deliberately tiny and the detailed rows now live in
    # SQLite.  Release the large extraction graph before constructing the FTS
    # database; this keeps broad-corpus rebuilds inside the VPS memory budget.
    del citation_index
    hierarchy = build_hierarchy(wiki_idx)
    graph = build_graph()
    git = build_git(
        official_transitions, transition_reviews, retrospective_candidates)
    decisions = load_decisions()
    data_policy = {
        "schema_version": 1,
        "public_build": not include_quarantined_sources(),
        "includes_quarantined_sources": include_quarantined_sources(),
        "excluded_snapshot_families": (
            [] if include_quarantined_sources()
            else sorted(QUARANTINED_SOURCES)),
        "reason": "third-party database/reuse permission not established",
        "buzer_role": "private_candidate_and_external_cross_check",
    }
    eu_index = build_eu_index()
    gii_catalog = build_gii_catalog(wiki_idx)
    amendment_fates = build_amendment_fates(details, built_at)
    watched = build_watched_procedures(hierarchy, amendment_fates, built_at)
    search_counts = build_search_database(
        details, WEB / "search.sqlite", ROOT / "data" / "search_synonyms.json")

    patches = load("patches", "patches.jsonl")
    gii_snapshot = latest_snapshot("gii")
    exact_state_events = official_state_transition_events(
        reviewed_transitions)
    federal_history = build_public_federal_history(
        patches,
        (read_jsonl(gii_snapshot / "norms.jsonl")
         if gii_snapshot and (gii_snapshot / "norms.jsonl").is_file()
         else []),
        ((SNAPSHOTS / "gii").iterdir()
         if (SNAPSHOTS / "gii").is_dir() else []),
        gii_snapshot.name if gii_snapshot else built_at[:10],
        exact_events=exact_state_events,
    )
    observations_by_act: dict[str, list[dict]] = {}
    for observation in official_states.get("observations") or []:
        observations_by_act.setdefault(
            str(observation.get("act_id") or ""), []).append(observation)
    transitions_by_act: dict[str, list[dict]] = {}
    for transition in official_transitions:
        transitions_by_act.setdefault(transition["act_id"], []).append(
            transition)
    reviews_by_act: dict[str, list[dict]] = {}
    for review in transition_reviews:
        reviews_by_act.setdefault(str(review.get("act_id") or ""), []).append(
            review)
    history_by_act: dict[str, list[dict]] = {}
    for event in federal_history["events"]:
        history_by_act.setdefault(str(event.get("act") or ""), []).append(
            event)
    for act in details.values():
        if act.get("juris") == "DE":
            act_id = str(act.get("id") or "")
            act["verified_history"] = history_by_act.get(
                str(act.get("jurabk") or ""), [])
            act["official_states"] = sorted(
                observations_by_act.get(act_id, []),
                key=lambda row: row["observed_at"])
            act["official_transition_reviews"] = sorted(
                reviews_by_act.get(act_id, []),
                key=lambda row: str(row.get("effective_at") or ""),
                reverse=True)
            observed_versions = []
            for transition in transitions_by_act.get(act_id, []):
                observed_versions.append({
                    "date": transition["observed_at"],
                    "observed_at": transition["observed_at"],
                    "previous_observed_at": transition[
                        "previous_observed_at"],
                    "date_basis": transition["date_basis"],
                    "verification": transition["verification"],
                    "text": "Amtlicher konsolidierter GII-Stand geändert",
                    "url": transition.get("source_url"),
                    "source_url": transition.get("source_url"),
                    "state_digest": transition["state_sha256"],
                    "previous_state_digest": transition[
                        "previous_state_sha256"],
                    "builddate": transition.get("new_builddate"),
                    "changes": [
                        {**change, "source": "gii_observed_state"}
                        for change in transition.get("changes") or []
                    ],
                })
            legal_versions = []
            for review in reviews_by_act.get(act_id, []):
                bgbl = dict(review.get("bgbl") or {})
                legal_versions.append({
                    "date": review["effective_at"],
                    "published_at": review["published_at"],
                    "effective_at": review["effective_at"],
                    "observed_at": review["observed_at"],
                    "previous_observed_at": review[
                        "previous_observed_at"],
                    "date_basis": review["date_basis"],
                    "verification": review["verification"],
                    "legal_effect_verified": True,
                    "legal_verification": review["verification"],
                    "review_id": review["id"],
                    "text": "Amtliche BGBl-Änderung · Inkrafttreten geprüft",
                    "url": bgbl.get("pdf_url"),
                    "source_url": bgbl.get("pdf_url"),
                    "state_digest": review["state_sha256"],
                    "previous_state_digest": review[
                        "previous_state_sha256"],
                    "procedure_id": review.get("procedure_id"),
                    "amending_articles": list(
                        review.get("amending_articles") or []),
                    "bgbl": bgbl,
                    "changes": [
                        {**change,
                         "effective_date": review["effective_at"],
                         "source": "official_bgbl_review"}
                        for change in review.get("changes") or []
                    ],
                })
            act["versions"].extend(observed_versions + legal_versions)
            act["versions"].sort(
                key=lambda row: str(row.get("date") or ""), reverse=True)
            retrospective_act = retrospective["acts"].get(act_id)
            if retrospective_act:
                act["retrospective"] = {
                    "available": True,
                    "history_start": retrospective_act.get("history_start"),
                    "current_intervals": sum(
                        row.get("knowledge_to") is None
                        for row in retrospective_act.get("intervals") or []),
                    "current_events": sum(
                        row.get("knowledge_to") is None
                        for row in retrospective_act.get("events") or []),
                    "coverage": retrospective_act.get("coverage") or {},
                }
    sts: dict[str, int] = {}
    for p_ in patches:
        sts[p_["status"]] = sts.get(p_["status"], 0) + 1
    bills = load("bay_landtag", "bills.jsonl")
    watched_procedures = [
        {key: row.get(key) for key in (
            "id", "watch_id", "source", "jurisdiction", "procedure",
            "title", "status", "stage", "date", "updated", "gesta", "url",
            "active", "terminal", "tracking_state", "last_checked",
            "last_changed", "validation_ids",
        )}
        for row in watched["procedures"]
    ]
    summary = {
        "built_at": built_at,
        "acts_fed": sum(1 for a in wiki_idx if a["juris"] == "DE"),
        "acts_by": sum(1 for a in wiki_idx if a["juris"] == "DE-BY"),
        "patches": sts,
        "vorgaenge": len(load("dip", "vorgaenge.jsonl")),
        "watched_procedures": watched_procedures,
        "watched_active": watched["active_count"],
        "watched_terminal": watched["terminal_count"],
        "amendment_fates": amendment_fates["total"],
        "amendment_fates_validated": amendment_fates["validated"],
        "verified_federal_events": federal_history["total"],
        "verified_federal_event_tiers": federal_history["tiers"],
        "official_federal_observations": official_states[
            "total_observations"],
        "official_federal_states": official_states["total_states"],
        "official_federal_transitions": official_states[
            "total_transitions"],
        "official_federal_legal_reviews": len(transition_reviews),
        "retrospective_history": retrospective["counts"],
        "verified_reconstructions": len(
            verified_reconstructions.get("reconstructions") or []),
        "bay_bills": len(bills),
        "bay_verkuendet": sum(1 for b in bills
                              if b["status"] == "verkuendet"),
        "eu_instruments": len(load("eu_layer", "instruments.jsonl")),
        "eu_index_total": eu_index["total"] if eu_index else 0,
        "gii_catalog_total": gii_catalog["total"] if gii_catalog else 0,
        "transpositions": len(load("eu_layer", "transpositions.jsonl")),
        "feed_events": len(feed),
        "decisions": len(decisions),
        "citations": citations["counts"],
        "data_policy": data_policy,
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
        "data_policy.json": dump("data_policy.json", data_policy),
        "feed.json": dump("feed.json", feed),
        "wiki.json": dump("wiki.json", wiki_idx),
        "decisions.json": dump("decisions.json", decisions),
        "citations.json": dump("citations.json", citations),
        "watched_procedures.json": dump("watched_procedures.json", watched),
        "amendment_fates.json": dump("amendment_fates.json", amendment_fates),
        "verified_federal_events.json": dump(
            "verified_federal_events.json", federal_history),
        "official_federal_states.json": dump(
            "official_federal_states.json", official_states),
        "official_transition_reviews.json": dump(
            "official_transition_reviews.json", {
                "schema_version": 1,
                "built_at": built_at,
                "source_policy": {
                    "official_only": True,
                    "effective_dates_inferred": False,
                    "gate": ("complete_gii_state_pair+final_bgbl_command+"
                             "dip_article_commencement"),
                },
                "total": len(transition_reviews),
                "reviews": transition_reviews,
            }),
        "retrospective_history.json": dump(
            "retrospective_history.json", retrospective),
        "verified_reconstructions.json": dump(
            "verified_reconstructions.json", public_reconstructions),
        "hierarchy.json": dump("hierarchy.json", hierarchy),
        "graph.json": dump("graph.json", graph),
        "git.json": dump("git.json", git),
    }
    sizes["citations.sqlite"] = (WEB / "citations.sqlite").stat().st_size
    if eu_index:
        sizes["eu_index.json"] = dump("eu_index.json", eu_index)
    else:
        (WEB / "eu_index.json").unlink(missing_ok=True)
    if gii_catalog:
        sizes["gii_catalog.json"] = dump("gii_catalog.json", gii_catalog)
    else:
        (WEB / "gii_catalog.json").unlink(missing_ok=True)
    for aid, d in details.items():
        dump(f"acts/{aid}.json", d)
    write_retrospective_sqlite(
        WEB / "retrospective_history.sqlite", retrospective)
    sizes["retrospective_history.sqlite"] = (
        WEB / "retrospective_history.sqlite").stat().st_size
    print(f"web data -> {WEB}")
    for k, v in sizes.items():
        print(f"  {k:16} {v/1024:8.1f} KB")
    print(f"  acts/*.json      {len(details)} files")
    print(f"  search.sqlite    {search_counts['acts']} acts / "
          f"{search_counts['norms']} norms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
