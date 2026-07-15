"""Build a QFS arena from the latest DIP snapshot — the live legislative
pipeline of a Wahlperiode as a navigable, time-scrubbable graph.

Mapping (docs/VISION.md):
  Vorgang            -> QNode (trust rises with status) + QBelief
                        "becomes law" (masses from beratungsstand,
                        bornTick = month of the latest procedural step)
  Initiator          -> QNode + CREATED_BY edge (hard, delta 1.0)
  Sachgebiet         -> QNode + thematic edge (soft, delta 0.3)
  month              -> QState tick (entropy = share of undecided bills)
  month transition   -> QTransition
  Wahlperiode        -> QWorld (stability = share of promulgated bills)

Open the result in qfs_visualizer: timeline mode replays the legislature;
belief masses separate geltendes Recht (pTrue) from pipeline noise (pNone)
and killed bills (pFalse) — the 12-Monatsfrist discipline, visual.

    python3 tools/build_qfs.py [--out data/lexgraph_de_wp21.qfs]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import re as _re
import sys
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from common import latest_snapshot, read_jsonl          # noqa: E402
from qfs import QfsWriter, parse_qfs                    # noqa: E402

# beratungsstand -> (pTrue, pFalse, pBoth, pNone, memory, source_trust)
STAND = {
    "Verkündet":                    (.95, .00, .00, .05, "long-term", 5),
    "Abgeschlossen - Ergebnis siehe Vorgangsablauf":
                                    (.60, .20, .10, .10, "working", 4),
    "Verabschiedet":                (.80, .02, .05, .13, "working", 4),
    "Beschlussempfehlung liegt vor":(.55, .05, .10, .30, "working", 3),
    "Überwiesen":                   (.30, .05, .05, .60, "working", 3),
    "Dem Bundestag zugeleitet - Noch nicht beraten":
                                    (.15, .05, .00, .80, "working", 3),
    "Dem Bundesrat zugeleitet - Noch nicht beraten":
                                    (.15, .05, .00, .80, "working", 3),
    "Noch nicht beraten":           (.10, .05, .00, .85, "ephemeral", 2),
    "1. Durchgang im Bundesrat abgeschlossen":
                                    (.35, .05, .05, .55, "working", 3),
    "Abgelehnt":                    (.02, .95, .00, .03, "dormant", 4),
    "Für erledigt erklärt":         (.02, .90, .00, .08, "dormant", 3),
    "Zurückgezogen":                (.02, .90, .00, .08, "dormant", 3),
}
DEFAULT_STAND = (.20, .10, .05, .65, "working", 2)
DEAD = {"Abgelehnt", "Für erledigt erklärt", "Zurückgezogen"}


def canon_eli(u: str) -> str:
    """DIP cites 'eli/bund/BGBl_1/2026/197', recht.bund.de and NeuRIS
    'eli/bund/bgbl-1/2026/197' — one join key for all three."""
    m = _re.search(r"eli/bund/([^/\s\"]+)/(\d{4})/(s?\d+)", u or "")
    if not m:
        return ""
    return f"eli/bund/{m.group(1).lower().replace('_', '-')}" \
           f"/{m.group(2)}/{m.group(3)}"


def month_ord(iso: str) -> int:
    y, m = int(iso[:4]), int(iso[5:7])
    return y * 12 + (m - 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "lexgraph_de_wp21.qfs"))
    args = ap.parse_args()

    snap = latest_snapshot("dip")
    if not snap:
        print("run pipeline/fetch_dip.py first", file=sys.stderr)
        return 1
    vorgaenge = list(read_jsonl(snap / "vorgaenge.jsonl"))
    gii = latest_snapshot("gii")
    acts = list(read_jsonl(gii / "acts.jsonl")) if gii else []
    ev_snap = latest_snapshot("bgbl_events")
    events = list(read_jsonl(ev_snap / "events.jsonl")) if ev_snap else []
    include_quarantined = os.environ.get(
        "LEXGRAPH_INCLUDE_QUARANTINED") == "1"
    bz = latest_snapshot("buzer") if include_quarantined else None
    versions = list(read_jsonl(bz / "versions.jsonl")) if bz else []
    upcoming = list(read_jsonl(bz / "upcoming.jsonl")) if bz else []
    nr = latest_snapshot("neuris_changelog")
    nr_events = list(read_jsonl(nr / "events.jsonl")) if nr else []
    pt = latest_snapshot("patches")
    patches = list(read_jsonl(pt / "patches.jsonl")) if pt else []
    _s = latest_snapshot("bayern_recht")
    bay_acts = list(read_jsonl(_s / "acts.jsonl")) if _s else []
    bay_versions = list(read_jsonl(_s / "versions.jsonl")) if _s else []
    _s = latest_snapshot("bay_landtag")
    bay_bills = list(read_jsonl(_s / "bills.jsonl")) if _s else []
    _s = latest_snapshot("gvbl_events")
    gvbl_events = list(read_jsonl(_s / "events.jsonl")) if _s else []
    _s = latest_snapshot("eu_layer")
    instruments = list(read_jsonl(_s / "instruments.jsonl")) if _s else []
    transpositions = list(read_jsonl(_s / "transpositions.jsonl"))         if _s else []
    _s = latest_snapshot("laender_monitor") if include_quarantined else None
    laender_ev = list(read_jsonl(_s / "events.jsonl")) if _s else []
    print(f"building from {len(vorgaenge)} Vorgänge, {len(acts)} acts, "
          f"{len(events)} promulgation events, {len(versions)} act "
          f"versions, {len(upcoming)} upcoming, {len(nr_events)} "
          f"consolidation events, {len(patches)} patch instructions")
    print(f"  + BY: {len(bay_acts)} acts/{len(bay_versions)} versions, "
          f"{len(bay_bills)} Landtag bills, {len(gvbl_events)} GVBl/BayMBl "
          f"events | EU: {len(instruments)} instruments, "
          f"{len(transpositions)} DEU transpositions | Länder: "
          f"{len(laender_ev)} monitor events")

    w = QfsWriter(arena_cap=8 << 20)
    w.add_dir(); w.add_cas()

    verb = w.add_node("WIRD_GESETZ", trust=3)
    bgbl = w.add_node("BGBl (verkündet)", trust=5)
    pipe = w.add_node("Gesetzgebungsverfahren", trust=3)

    initiators: dict[str, int] = {}
    topics: dict[str, int] = {}
    months: set[int] = set()
    rows = []

    # corpus acts (GII HEAD) as high-trust anchor nodes
    act_nodes: dict[str, int] = {}      # slug -> node off
    act_match: list[tuple[str, str]] = []   # (lowered needle, slug)
    for a in acts:
        label = f"{a['jurabk']} — {a['long_title'][:48]}" if a.get(
            'long_title') else a['jurabk']
        act_nodes[a['slug']] = w.add_node(label[:70], trust=5)
        lt = (a.get('long_title') or '').lower()
        if len(lt) > 14:
            act_match.append((lt.rstrip('gesetz') if lt.endswith('gesetz')
                              else lt, a['slug']))
        jb = (a.get('jurabk') or '').lower()
        if len(jb) >= 4:
            act_match.append((jb, a['slug']))

    vg_node_by_id: dict[str, int] = {}
    jurabk_node = {a["jurabk"]: act_nodes[a["slug"]]
                   for a in acts if a.get("jurabk")}
    # extracted PatchInstructions: vorgang -> {target jurabk: #commands}
    patched: dict[str, dict[str, int]] = {}
    for p in patches:
        d = patched.setdefault(p["procedure"].split(":")[-1], {})
        d[p["target_act"]] = d.get(p["target_act"], 0) + 1

    for vg in vorgaenge:
        datum = vg.get("datum") or "2025-01-01"
        tick = month_ord(datum)
        months.add(tick)
        stand = vg.get("beratungsstand") or "?"
        pt, pf, pb, pn, mem, st = STAND.get(stand, DEFAULT_STAND)
        titel = (vg.get("titel") or "?").strip()
        label = titel if len(titel) <= 70 else titel[:67] + "…"
        node = w.add_node(label, trust=5 if stand == "Verkündet" else
                          (2 if stand in DEAD else 3))
        for ini in (vg.get("initiative") or [])[:3]:
            if ini not in initiators:                 # setdefault would
                initiators[ini] = w.add_node(ini, trust=4)   # eagerly create
            io = initiators[ini]
            w.add_edge([(node, 1.0), (io, -1.0)], reltype=10,   # CREATED_BY
                       delta=1.0, trust=4)
        for sg in (vg.get("sachgebiet") or [])[:2]:
            if sg not in topics:
                topics[sg] = w.add_node(sg, trust=3)
            to = topics[sg]
            w.add_edge([(node, 0.0), (to, 0.0)], reltype=40,    # thematic
                       delta=0.3, trust=3)
        ex = patched.get(str(vg.get("id")))
        if ex:
            # parsed Änderungsbefehle -> precise AMENDS edges; delta
            # scales gently with command count (log-ish via min)
            for jurabk, n_cmd in ex.items():
                tgt = jurabk_node.get(jurabk)
                if tgt is not None:
                    w.add_edge([(node, 1.0), (tgt, -1.0)],
                               reltype=21,          # AMENDS (extracted)
                               delta=min(1.0, 0.7 + n_cmd / 100),
                               trust=4)
        else:
            # fallback heuristic: bill title mentions a corpus act
            low = titel.lower()
            hit = {slug for needle, slug in act_match if needle in low}
            for slug in list(hit)[:3]:
                w.add_edge([(node, 1.0), (act_nodes[slug], -1.0)],
                           reltype=20,      # AMENDS_CANDIDATE (heuristic!)
                           delta=0.6, trust=2)
        rows.append((vg, node, tick, (pt, pf, pb, pn, mem, st), stand))
        vg_node_by_id[str(vg.get("id"))] = node

    for vg, node, tick, (pt, pf, pb, pn, mem, st), stand in rows:
        w.add_belief(claim_key=int(vg.get("id", 0) or 0),
                     subject=node, relation=verb,
                     obj=bgbl if stand == "Verkündet" else pipe,
                     p_true=pt, p_false=pf, p_both=pb, p_none=pn,
                     born_tick=tick, memory=mem, source_trust=st)

    # promulgation events joined to Vorgänge via ELI
    eli_of = {}
    for vg, node, tick, _, stand in rows:
        for vk in (vg.get("verkuendung") or []):
            key = canon_eli(vk.get("pdf_url") or "")
            if key:
                eli_of[key] = node
    n_pub = 0
    for e in events:
        key = canon_eli(e.get("eli") or "")
        if key and key in eli_of and e.get("time"):
            ev_node = w.add_node(
                (e.get("bgbl_citation") or "BGBl")[:40], trust=5)
            w.add_edge([(eli_of[key], 1.0), (ev_node, -1.0)],
                       reltype=30,          # PUBLISHED_IN
                       delta=1.0, trust=5)
            n_pub += 1
    if n_pub:
        print(f"  {n_pub} PUBLISHED_IN edges via ELI join")

    # ---- back-history: buzer version chains as belief revisions --------
    # each entry = "this act's text changed, in force <date>" -> a belief
    # born at that month; the timeline now reaches back to 2006 and every
    # corpus act pulses on its amendment dates (source_trust 3: buzer is
    # non-authoritative — convenience mirror, never outranks BGBl)
    node_by_jurabk = {}
    for a in acts:
        if a.get("jurabk"):
            node_by_jurabk[a["jurabk"]] = act_nodes[a["slug"]]
    amended = w.add_node("TEXTÄNDERUNG in Kraft", trust=3)
    n_ver, seq = 0, {}
    for v in sorted(versions, key=lambda x: x.get("date") or ""):
        node = node_by_jurabk.get(v.get("jurabk"))
        d = v.get("date") or ""
        if node is None or len(d) < 10:
            continue
        tick = month_ord(d)
        months.add(tick)
        seq[node] = seq.get(node, 0) + 1
        w.add_belief(claim_key=v["act_id"] * 10_000 + seq[node],
                     subject=node, relation=amended, obj=bgbl,
                     p_true=.90, p_false=.00, p_both=.00, p_none=.10,
                     born_tick=tick,
                     memory="long-term" if d >= "2020" else "dormant",
                     source_trust=3)
        n_ver += 1

    # anticipation: promulgated but not yet in force (buzer /v.htm, ~100d
    # horizon) -> beliefs born on FUTURE ticks; join via act_id
    jurabk_by_actid = {v["act_id"]: v["jurabk"] for v in versions}
    n_up = 0
    for u in upcoming:
        m = _re.search(r"/gesetz/(\d+)/", u.get("url") or "")
        node = node_by_jurabk.get(
            jurabk_by_actid.get(int(m.group(1)) if m else -1, ""))
        d = u.get("date") or ""
        if node is None or len(d) < 10:
            continue
        tick = month_ord(d)
        months.add(tick)
        seq[node] = seq.get(node, 0) + 1
        w.add_belief(claim_key=int(m.group(1)) * 10_000 + seq[node],
                     subject=node, relation=amended, obj=pipe,
                     p_true=.85, p_false=.02, p_both=.00, p_none=.13,
                     born_tick=tick, memory="working", source_trust=3)
        n_up += 1

    # NeuRIS consolidation revisions joined to promulgated Vorgänge via
    # work-ELI (only recent laws overlap — NeuRIS work ELIs are original
    # promulgation ELIs, so pre-WP21 works have no DIP counterpart)
    konsol = w.add_node("KONSOLIDIERT (NeuRIS)", trust=4)
    n_kon = 0
    for e in nr_events:
        node = eli_of.get(canon_eli(e.get("eli_work") or ""))
        t = e.get("time") or ""
        if node is None or len(t) < 7:
            continue
        tick = month_ord(t)
        months.add(tick)
        w.add_belief(claim_key=zlib.crc32(e["event_id"].encode()),
                     subject=node, relation=konsol, obj=bgbl,
                     p_true=.90, p_false=.00, p_both=.00, p_none=.10,
                     born_tick=tick, memory="working", source_trust=4)
        n_kon += 1
    print(f"  {n_ver} version beliefs (buzer), {n_up} anticipation "
          f"beliefs, {n_kon} consolidation beliefs (NeuRIS)")

    # ================= Bavaria world (DE-BY) ==========================
    gvbl_hub = w.add_node("GVBl/BayMBl (Bayern)", trust=5)
    by_amended = w.add_node("TEXTÄNDERUNG (BayRS)", trust=4)
    by_nodes: dict[str, int] = {}
    bayrs_of: dict[str, str] = {}
    for a in bay_acts:
        label = f"BY {a['jurabk']} — {(a.get('long_title') or '')[:44]}"
        by_nodes[a["jurabk"]] = w.add_node(label[:70], trust=5)
        if a.get("bayrs_nr"):
            bayrs_of[a["bayrs_nr"]] = a["jurabk"]

    # amendment back-history: ffn register + XML aenderungsverlauf carry
    # the same chain in different granularity — dedupe on (jurabk, date),
    # XML wins (it names the amending act)
    # cross-source merge only: several DISTINCT amendments can share an
    # in-force date (omnibus days), so count occurrences per source and
    # emit max(ffn, xml) beliefs per (jurabk, date)
    occ: dict[tuple[str, str], dict[str, int]] = {}
    for v in bay_versions:
        d = v.get("date") or ""
        if len(d) < 10 or v["jurabk"] not in by_nodes:
            continue
        per = occ.setdefault((v["jurabk"], d), {})
        src = v.get("source") or "?"
        per[src] = per.get(src, 0) + 1
    n_byver = 0
    for (jb, d), per in occ.items():
        t = month_ord(d)
        months.add(t)
        for k in range(max(per.values())):
            w.add_belief(claim_key=zlib.crc32(
                             f"byver:{jb}:{d}:{k}".encode()),
                         subject=by_nodes[jb], relation=by_amended,
                         obj=gvbl_hub, p_true=.90, p_false=.00,
                         p_both=.00, p_none=.10, born_tick=t,
                         memory="long-term" if d >= "2020" else "dormant",
                         source_trust=4)
            n_byver += 1

    # GVBl/BayMBl promulgation events joined to corpus acts via BayRS
    # Gliederungsnummer (exact token match)
    n_gvbl_join = 0
    for e in gvbl_events:
        gl = e.get("gliederungs_nr") or ""
        d = e.get("time") or ""
        for tok in (x.strip() for x in gl.split(",")):
            jb = bayrs_of.get(tok)
            if jb and len(d) >= 7:
                t = month_ord(d)
                months.add(t)
                w.add_belief(claim_key=zlib.crc32(
                                 f"{e['event_id']}:{jb}".encode()),
                             subject=by_nodes[jb], relation=by_amended,
                             obj=gvbl_hub, p_true=.95, p_false=.00,
                             p_both=.00, p_none=.05, born_tick=t,
                             memory="working", source_trust=5)
                n_gvbl_join += 1

    # Landtag WP19 pipeline: bills with lifecycle-derived status ladder
    BYSTAND = {
        "verkuendet":     (.95, .00, .00, .05, "long-term", 5),
        "beschlossen":    (.85, .02, .03, .10, "working", 4),
        "2_lesung":       (.50, .08, .05, .37, "working", 3),
        "ausschuss":      (.35, .10, .05, .50, "working", 3),
        "1_lesung":       (.25, .10, .05, .60, "working", 3),
        "eingebracht":    (.15, .10, .00, .75, "ephemeral", 2),
        "abgelehnt":      (.02, .95, .00, .03, "dormant", 4),
        "zurueckgezogen": (.02, .90, .00, .08, "dormant", 3),
    }
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
    n_bybill_edges = 0
    for b in bay_bills:
        st = b.get("status") or "eingebracht"
        pt_, pf_, pb_, pn_, mem_, str_ = BYSTAND.get(
            st, (.20, .10, .05, .65, "working", 2))
        dates = [ev["date"] for ev in (b.get("lifecycle") or [])
                 if ev.get("date")] or [b.get("eingang") or "2024-01-01"]
        d = max(dates)
        t = month_ord(d)
        months.add(t)
        titel = (b.get("titel") or "?").strip()
        node = w.add_node(("BY↯ " + titel)[:70],
                          trust=5 if st == "verkuendet" else
                          (2 if st in ("abgelehnt", "zurueckgezogen")
                           else 3))
        w.add_belief(claim_key=zlib.crc32(
                         f"bybill:{b.get('gegenstandid')}".encode()),
                     subject=node, relation=verb,
                     obj=gvbl_hub if st == "verkuendet" else pipe,
                     p_true=pt_, p_false=pf_, p_both=pb_, p_none=pn_,
                     born_tick=t, memory=mem_, source_trust=str_)
        low = titel.lower()
        for jb, needle in BY_NEEDLES.items():
            tgt = by_nodes.get(jb)       # act may be absent in a degraded
            if needle in low and tgt is not None:      # snapshot
                w.add_edge([(node, 1.0), (tgt, -1.0)],
                           reltype=20,      # AMENDS_CANDIDATE (title)
                           delta=0.7, trust=3)
                n_bybill_edges += 1
        if st == "verkuendet":
            w.add_edge([(node, 1.0), (gvbl_hub, -1.0)],
                       reltype=30,          # PUBLISHED_IN
                       delta=1.0, trust=5)
    print(f"  BY: {n_byver} version beliefs, {n_gvbl_join} GVBl↔BayRS "
          f"joins, {len(bay_bills)} bills, {n_bybill_edges} bill→act "
          f"edges")

    # ================= EU world (genesis plane) =======================
    oj_hub = w.add_node("Amtsblatt der EU (OJ L)", trust=5)
    eu_nodes: dict[str, int] = {}
    n_genesis = n_transp = 0
    for ins in instruments:
        kind = "RL" if ins["kind"] == "directive" else "VO"
        label = (f"EU {kind} {ins['year']}/{ins['number']} — "
                 f"{(ins.get('title') or '')[:40]}")
        node = w.add_node(label[:70], trust=5)
        eu_nodes[ins["celex"]] = node
        d = ins.get("in_force_date") or ins.get("pub_date") or ""
        if d < "1900":                   # CELLAR sentinel (1001-01-01)
            d = ins.get("pub_date") or ""
        if len(d) >= 7 and d >= "1900":
            t = month_ord(d)
            months.add(t)
            inf = ins.get("in_force")
            if inf is True:              # geltendes EU-Recht
                masses = (.95, .00, .00, .05)
            elif inf is False:           # repealed — a dead status
                masses = (.05, .85, .00, .10)
            else:                        # metadata missing
                masses = (.60, .00, .00, .40)
            w.add_belief(claim_key=zlib.crc32(ins["celex"].encode()),
                         subject=node, relation=verb, obj=oj_hub,
                         p_true=masses[0], p_false=masses[1],
                         p_both=masses[2], p_none=masses[3],
                         born_tick=t, memory="long-term", source_trust=5)
        # genesis: German bill implements/executes the EU instrument
        for vid in (ins.get("dip_vorgang_ids") or []):
            vn = vg_node_by_id.get(str(vid))
            if vn is not None:
                w.add_edge([(vn, 1.0), (node, -1.0)],
                           reltype=60 if kind == "RL" else 61,
                           delta=0.9, trust=4)
                n_genesis += 1
    # transposition state: directive already carried into German law
    mne_count: dict[str, int] = {}
    for tr in transpositions:
        mne_count[tr["directive_celex"]] =             mne_count.get(tr["directive_celex"], 0) + 1
    for celex, n in mne_count.items():
        node = eu_nodes.get(celex)
        if node is not None:
            w.add_edge([(node, 1.0), (bgbl, -1.0)],
                       reltype=63,          # TRANSPOSED_DEU
                       delta=min(1.0, .5 + n / 40), trust=5)
            n_transp += 1
    print(f"  EU: {len(eu_nodes)} instruments, {n_genesis} genesis edges "
          f"(Umsetzung/Durchführung), {n_transp} DEU transposition edges")

    # ================= Länder monitor (16 Landtage) ===================
    # Do not even emit the monitor hub in a public build.  A dangling label
    # would still disclose a quarantined source family and used to survive in
    # graph.json after the underlying rows were excluded.
    topic = (w.add_node("Asyl/Migration (Länder-Monitor)", trust=3)
             if laender_ev else None)
    land_nodes: dict[str, int] = {}
    land_count: dict[str, int] = {}
    for e in laender_ev:
        assert topic is not None
        code = (e.get("jurisdiction") or "DE-?")[3:]
        if code not in land_nodes:
            land_nodes[code] = w.add_node(f"Landtag {code}", trust=3)
        land_count[code] = land_count.get(code, 0) + 1
        d = e.get("datum") or ""
        if len(d) >= 7:
            t = month_ord(d)
            months.add(t)
            w.add_belief(claim_key=zlib.crc32(e["event_id"].encode()),
                         subject=land_nodes[code], relation=verb,
                         obj=topic, p_true=.50, p_false=.00, p_both=.00,
                         p_none=.50, born_tick=t, memory="ephemeral",
                         source_trust=2)
    for code, node in land_nodes.items():
        assert topic is not None
        w.add_edge([(node, 0.0), (topic, 0.0)], reltype=40,
                   delta=min(1.0, land_count[code] / 20), trust=2)
    print(f"  Länder: {len(land_nodes)} Landtage active, "
          f"{sum(land_count.values())} monitor beliefs")

    ticks = sorted(months)
    tick_index = {t: i for i, t in enumerate(ticks)}

    # month states: entropy = share of still-undecided bills known by then
    states = {}
    prev = 0
    for t in ticks:
        known = [r for r in rows if r[2] <= t]
        undecided = [r for r in known
                     if r[4] not in DEAD and r[4] != "Verkündet"]
        entropy = round(len(undecided) / max(1, len(known)), 3)
        st_off = w.add_state(state_id=tick_index[t] + 1, tick=t, parent=prev,
                             entropy=entropy,
                             contradiction=round(
                                 sum(1 for r in known if r[3][2] > .05)
                                 / max(1, len(known)), 3))
        if prev:
            w.add_transition(from_state=prev, to_state=st_off, policy=3)
        states[t] = st_off
        prev = st_off

    n_ext = sum(1 for vid in patched if any(
        jurabk_node.get(j) is not None for j in patched[vid]))
    print(f"  {n_ext} Vorgänge with extracted AMENDS edges")

    verk = sum(1 for r in rows if r[4] == "Verkündet")
    w.add_world(world_id=1, tick=ticks[-1] if ticks else 0,
                observed_state=prev,
                stability=round(verk / max(1, len(rows)), 3),
                contradiction_level=round(
                    sum(1 for r in rows if r[3][2] > .05) / max(1, len(rows)), 3),
                truth_posterior=round(verk / max(1, len(rows)), 3),
                source_trust=4)
    if bay_bills:
        by_verk = sum(1 for b in bay_bills
                      if b.get("status") == "verkuendet")
        w.add_world(world_id=2, tick=ticks[-1] if ticks else 0,
                    observed_state=prev,
                    stability=round(by_verk / max(1, len(bay_bills)), 3),
                    contradiction_level=0.0,
                    truth_posterior=round(
                        by_verk / max(1, len(bay_bills)), 3),
                    source_trust=5)
    if instruments:
        in_force = sum(1 for i in instruments if i.get("in_force"))
        w.add_world(world_id=3, tick=ticks[-1] if ticks else 0,
                    observed_state=prev,
                    stability=round(in_force / max(1, len(instruments)), 3),
                    contradiction_level=0.0,
                    truth_posterior=round(
                        in_force / max(1, len(instruments)), 3),
                    source_trust=5)

    n = w.write(args.out)
    arena_bytes = Path(args.out).read_bytes()
    p = parse_qfs(arena_bytes)
    policy = {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "arena": Path(args.out).name,
        "qfs_sha256": hashlib.sha256(arena_bytes).hexdigest(),
        "public_build": not include_quarantined,
        "includes_quarantined_sources": include_quarantined,
        "included_snapshot_families": [
            "dip", "gii", "bgbl_events", "neuris_changelog", "patches",
            "bayern_recht", "bay_landtag", "gvbl_events", "eu_layer",
        ] + (["buzer", "laender_monitor"] if include_quarantined else []),
    }
    policy_path = Path(f"{args.out}.policy.json")
    policy_tmp = policy_path.with_suffix(policy_path.suffix + ".tmp")
    policy_tmp.write_text(
        json.dumps(policy, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    policy_tmp.replace(policy_path)
    print(f"wrote {args.out} ({n:,} B)")
    print(f"  policy: {policy_path} "
          f"({'private' if include_quarantined else 'public'})")
    print(f"  validated: {p.counts}")
    print(f"  timeline: {len(ticks)} month ticks "
          f"({ticks and date(ticks[0]//12, ticks[0]%12+1, 1)} … "
          f"{ticks and date(ticks[-1]//12, ticks[-1]%12+1, 1)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
