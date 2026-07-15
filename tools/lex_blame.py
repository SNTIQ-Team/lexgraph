"""`git blame` / `git checkout` over the Lexgraph snapshots.

    python3 tools/lex_blame.py blame AsylbLG 3a
    python3 tools/lex_blame.py blame AufnG 4
    python3 tools/lex_blame.py checkout AsylG --at 2026-07-13 --norm 29a

Read-only over data/snapshots — NO network, NO snapshot writes.

blame ACT REF     every known amendment/patch touching one §/Art.,
                  newest first, across three tiers:
                    buzer     private federal candidates 2006+ (affected-§
                              list parsed from the synopsis title:
                              "für § 1 , § 1a , § 11 Artikel 4 <act>";
                              GG rows use "Artikel 104b" tokens);
                              non-authoritative; visible only with
                              LEXGRAPH_INCLUDE_QUARANTINED=1
                    amtlich   Bavarian BayRS: ffn Fortführungsnachweis
                              merged with the XML aenderungsverlauf on
                              the GVBl page (same key as lex_log.py) —
                              only the ffn description names affected
                              Art./§§ ("Art. 4 und 5 Abs. 3 geänd.")
                    pipeline  extracted Bundestag patch commands
                              (ref.para match); proposed/adopted ones
                              are marked NOT geltendes Recht — the
                              VISION acceptance discipline
                  Version rows naming no §/Art. at all ("mehrfach
                  geänd.", Neufassung) are kept as "? unspezif." —
                  they MAY touch REF; hiding them would be dishonest.

checkout ACT --at YYYY-MM-DD [--norm REF]
                  A federal checkout succeeds only on an exact official GII
                  retrieval observation and emits the complete law (or one
                  norm) as Markdown, including its content hash/provenance.
                  The retrieval date is explicitly not called an effective
                  date. Bavaria uses its official ffn/XML chain. Private mode
                  can inspect a Buzer candidate when no exact observation is
                  available, but never replaces official evidence.

Acts resolve case-insensitively via jurabk (federal gii acts.jsonl
first, then Bavarian bayern_recht acts.jsonl); REF accepts "3a",
"§ 3a", "Art. 4" alike.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
from api.act_archive import (  # noqa: E402
    ArchiveRequestError,
    render_markdown_snapshot,
)
from common import latest_snapshot, read_jsonl          # noqa: E402
from official_cli import (  # noqa: E402
    exact_observed_state,
    load_official_act_history,
)
from official_states import DEFAULT_STORE, StateStoreError  # noqa: E402

BADGE = {"proposed": "○ proposed", "adopted": "◑ adopted",
         "published": "● published", "rejected": "✗ rejected",
         "not_merged": "✗ not merged"}
PENDING = ("proposed", "adopted")

# one affected-norm token in a buzer title: "§ 1a ", "Artikel 104b "
TOKEN_RE = re.compile(r"(?:§|Artikel|Art\.)\s*(\d+[a-z]{0,3})\b"
                      r"(\s*\(neu\))?\s*")
LIST_START_RE = re.compile(r"\bfür\s+(?=§|Artikel\s*\d|Art\.\s*\d)")
COMMA_RE = re.compile(r",\s*")
# Bavarian ffn list: "Art. 4, 5 und 8", "Art, 7" (ffn typo), "§§ 23, 30"
BAY_LIST_RE = re.compile(r"(?:Art[.,]|Artikel|§§?)\s*"
                         r"(\d+[a-z]?(?:\s*(?:,|und|bis)\s*\d+[a-z]?)*)")
PAGE_RE = re.compile(r"S\.\s*(\d+)")
DATE_ARG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OFFICIAL_STORE = DEFAULT_STORE


def load(source: str, name: str) -> list[dict]:
    snap = latest_snapshot(source)
    return list(read_jsonl(snap / name)) if snap else []


def include_private_candidates() -> bool:
    return os.environ.get("LEXGRAPH_INCLUDE_QUARANTINED") == "1"


def official_history(jurabk: str) -> dict[str, list[dict]]:
    """Integrity-checked official rows; factored for focused CLI tests."""
    return load_official_act_history(jurabk, store=OFFICIAL_STORE)


def pick_act(query: str, acts: list[dict]) -> dict | None:
    q = query.lower().replace("_", " ").strip()
    for a in acts:
        if a["jurabk"].lower() == q or a.get("slug") == query.lower():
            return a
    for a in acts:
        if q in a["jurabk"].lower() or \
                q in (a.get("long_title") or "").lower():
            return a
    return None


def resolve_act(query: str) -> tuple[dict | None, str]:
    """(act, corpus) — federal corpus wins on jurabk collisions."""
    act = pick_act(query, load("gii", "acts.jsonl"))
    if act:
        return act, "federal"
    act = pick_act(query, load("bayern_recht", "acts.jsonl"))
    if act:
        return act, "bavarian"
    return None, ""


def norm_ref(s: str | None) -> str:
    """'§ 3a' / 'Art. 4' / '3A' -> '3a' / '4'."""
    s = (s or "").strip().lower()
    return re.sub(r"^(§+|artikel|art\.?)\s*", "", s).strip()


def clean_title(t: str) -> str:
    """Drop buzer boilerplate: leading date(s) + 'Synopse gesamt …'."""
    t = re.sub(r"^\d{2}\.\d{2}\.\d{4}\s*(\(\d{2}\.\d{2}\.\d{4}\)\s*)?",
               "", t)
    t = t.replace("Synopse gesamt oder einzeln für", " ")
    t = t.replace("Synopse gesamt", " ")
    return " ".join(t.split())


def parse_buzer_title(title: str) -> tuple[list[tuple[str, bool]], str]:
    """-> ([(ref, is_neu)], amending-act text).

    The affected list sits between 'für' and the amending act; tokens
    are comma-separated, the amending marker ('Artikel 4 GEAS-…') is
    NOT preceded by a comma — the scanner stops exactly there.
    """
    m = LIST_START_RE.search(title)
    if not m:
        return [], clean_title(title)
    pos = m.end()
    toks: list[tuple[str, bool]] = []
    while True:
        tm = TOKEN_RE.match(title, pos)
        if not tm:
            break
        toks.append((tm.group(1).lower(), bool(tm.group(2))))
        pos = tm.end()
        cm = COMMA_RE.match(title, pos)
        if not cm:
            break
        pos = cm.end()
    return toks, title[pos:].strip() or clean_title(title)


def parse_bay_refs(desc: str) -> list[str]:
    """Affected Art./§ numbers from a ffn description; expands
    'Art. 2 bis 5' ranges, splits ',' and 'und' enumerations."""
    refs: list[str] = []
    for m in BAY_LIST_RE.finditer(desc or ""):
        parts = re.split(r"\s*(,|und|bis)\s*", m.group(1))
        for i in range(0, len(parts), 2):
            n = parts[i].lower()
            prev = refs[-1] if refs else ""
            if i >= 2 and parts[i - 1] == "bis" and prev.isdigit() \
                    and n.isdigit() and 0 < int(n) - int(prev) <= 50:
                refs.extend(str(k)
                            for k in range(int(prev) + 1, int(n) + 1))
            else:
                refs.append(n)
    seen: set[str] = set()
    return [r for r in refs if not (r in seen or seen.add(r))]


def bavarian_events(jurabk: str) -> list[dict]:
    """ffn + xml version rows merged per amendment event (keyed on
    date + GVBl page, exactly like lex_log.py's dedupe). refs come
    from the ffn description only — the xml description names the
    amending act, whose § numbers are NOT affected articles."""
    groups: dict[tuple, dict] = {}
    for v in load("bayern_recht", "versions.jsonl"):
        if v["jurabk"] != jurabk or not v.get("date"):
            continue
        pm = PAGE_RE.search(v.get("gvbl_citation") or "")
        k = (v["date"],
             pm.group(1) if pm else (v.get("description") or "")[:40])
        groups.setdefault(k, {}).setdefault(v.get("source") or "ffn", v)
    events = []
    for g in groups.values():
        ffn, xml = g.get("ffn"), g.get("xml")
        desc = (ffn or {}).get("description") or ""
        events.append({
            "date": (ffn or xml)["date"],
            "refs": parse_bay_refs(desc),
            "changed": desc,
            "act": (xml or {}).get("description") or "",
            "cite": (ffn or {}).get("gvbl_citation")
                    or (xml or {}).get("gvbl_citation") or ""})
    events.sort(key=lambda e: e["date"])
    return events


def subloc(ref: dict) -> str:
    parts = []
    for key, lab in (("absatz", "Abs."), ("satz", "Satz"),
                     ("nummer", "Nr."), ("buchstabe", "Buchst.")):
        if ref.get(key):
            parts.append(f"{lab} {ref[key]}")
    return " ".join(parts)


def show_event(ev: dict) -> None:
    print(f"  {ev['label']:>10}  {ev['badge']}")
    for line in ev["lines"]:
        print(f"              {line}")


def patch_events(jurabk: str, ref: str) -> list[dict]:
    """Pipeline patch commands on §REF, grouped per procedure+status."""
    mine = [p for p in load("patches", "patches.jsonl")
            if p["target_act"].lower() == jurabk.lower()
            and norm_ref(p["ref"].get("para")) == ref]
    groups: dict[tuple, list[dict]] = {}
    for p in mine:
        groups.setdefault((p["procedure"], p["status"]), []).append(p)
    events = []
    for (_, status), ps in groups.items():
        p = ps[0]
        d = p.get("valid_from") or p.get("published_at") \
            or p.get("decided_at")
        badge = f"{BADGE[status]:12} [pipeline]"
        if status in PENDING:
            badge += " — NOT geltendes Recht"
        elif status == "published":
            badge += " — draft command; final attribution unverified"
        locs = sorted({subloc(x["ref"]) for x in ps} - {""})
        lines = [p["procedure_title"][:64]]
        loc = "; ".join(locs[:4]) + (" …" if len(locs) > 4 else "")
        lines.append(f"{len(ps)} patch cmd(s) on § {p['ref']['para']}"
                     + (f" ({loc})" if loc else ""))
        lines.append(f"[{p['source_doc']}] "
                     f"{(p.get('beratungsstand') or '')[:48]}".rstrip())
        events.append({"sort": d or "9999-99-99", "label": d or "pending",
                       "badge": badge, "lines": lines,
                       "kind": "pipeline"})
    return events


def official_norm_events(jurabk: str, ref: str) -> list[dict]:
    """Observed state-pair diffs and accepted legal events, kept separate."""
    history = official_history(jurabk)
    events: list[dict] = []
    for transition in history["transitions"]:
        changes = [change for change in transition.get("changes") or []
                   if norm_ref(change.get("para")) == ref]
        if not changes:
            continue
        operations = ", ".join(sorted({str(change.get("operation"))
                                       for change in changes}))
        events.append({
            "sort": transition["observed_at"],
            "label": transition["observed_at"],
            "badge": "◆ observed  [GII exact state pair] — NOT effective date",
            "lines": [
                f"{len(changes)} complete norm diff(s): {operations}",
                (f"state {transition['previous_state_sha256'][:12]} → "
                 f"{transition['state_sha256'][:12]}"),
                str(transition.get("source_url") or ""),
            ],
            "kind": "observed",
        })

    for review in history["reviews"]:
        changes = [change for change in review.get("changes") or []
                   if norm_ref(change.get("para")) == ref]
        if not changes:
            continue
        bgbl = review.get("bgbl") or {}
        lines = [
            (f"final BGBl command verified; published "
             f"{review['published_at']}"),
            (f"{bgbl.get('document_id') or 'BGBl'} · "
             f"Art. {', '.join(review.get('amending_articles') or [])}"),
        ]
        if bgbl.get("pdf_url"):
            lines.append(str(bgbl["pdf_url"]))
        if review.get("procedure_id"):
            lines.append(
                f"https://dip.bundestag.de/vorgang/{review['procedure_id']}")
        events.append({
            "sort": review["effective_at"],
            "label": review["effective_at"],
            "badge": "● effective [BGBl final text + DIP commencement]",
            "lines": lines,
            "kind": "amended",
        })
    return events


def cmd_blame(args: argparse.Namespace) -> int:
    act, corpus = resolve_act(args.act)
    if not act:
        print(f"no corpus act matches {args.act!r}", file=sys.stderr)
        return 1
    ref = norm_ref(args.ref)
    if not ref:
        print(f"cannot parse ref {args.ref!r}", file=sys.stderr)
        return 1
    jb = act["jurabk"]
    events: list[dict] = []

    if corpus == "federal":
        sym = "Artikel" if jb == "GG" else "§"
        try:
            events += official_norm_events(jb, ref)
        except StateStoreError as exc:
            print(f"official state history failed integrity checks: {exc}",
                  file=sys.stderr)
            return 1
        if include_private_candidates():
            for v in load("buzer", "versions.jsonl"):
                if v["jurabk"] != jb:
                    continue
                toks, amending = parse_buzer_title(v["title"])
                names = [t for t, _ in toks]
                if names and ref not in names:
                    continue
                if names:
                    badge = "● amended   [buzer — non-authoritative]"
                    if dict(toks)[ref]:
                        badge += f"  ({sym} {ref} NEU eingefügt)"
                else:
                    badge = "? unspezif. [buzer — no §-list in title]"
                shown = [r + (" (neu)" if neu else "") for r, neu in toks]
                lines = [amending[:64]]
                if shown:
                    lines.append(f"touches {sym} " + ", ".join(shown[:12])
                                 + (" …" if len(shown) > 12 else ""))
                lines.append(v["synopsis_url"])
                events.append({"sort": v["date"], "label": v["date"],
                               "badge": badge, "lines": lines,
                               "kind": "amended" if names else "unspec"})
        events += patch_events(jb, ref)
    else:
        bay = bavarian_events(jb)
        sym = "Art." if any("art" in (e["changed"] or "").lower()
                            for e in bay) else "§"
        for e in bay:
            if e["refs"] and ref not in e["refs"]:
                continue
            if e["refs"]:
                badge = "● amended   [amtlich — BayRS ffn/XML]"
            else:
                badge = "? unspezif. [amtlich — names no Art./§]"
            lines = [e["changed"][:64] or "(no ffn description)"]
            if e["act"]:
                lines.append(e["act"][:64])
            if e["cite"]:
                lines.append(f"({e['cite']})")
            events.append({"sort": e["date"], "label": e["date"],
                           "badge": badge, "lines": lines,
                           "kind": "amended" if e["refs"] else "unspec"})
        events += patch_events(jb, ref)   # no-op: patches are federal

    title = act.get("long_title") or ""
    print(f"== blame {jb} {sym} {ref} — {title[:44]}".rstrip())
    n_am = sum(1 for e in events if e["kind"] == "amended")
    n_ob = sum(1 for e in events if e["kind"] == "observed")
    n_pi = sum(1 for e in events if e["kind"] == "pipeline")
    n_un = sum(1 for e in events if e["kind"] == "unspec")
    print(f"   {corpus} corpus · {n_am} verified amendment(s) · "
          f"{n_ob} observed state transition(s) · "
          f"{n_pi} pipeline patch(es) · {n_un} unspecific")
    if not events:
        print(f"\n  no known amendment or patch touches {sym} {ref} "
              f"(back-history window only — silence is not proof)")
        return 0
    events.sort(key=lambda e: e["sort"], reverse=True)
    print()
    for ev in events:
        show_event(ev)
    return 0


def cmd_checkout(args: argparse.Namespace) -> int:
    at = args.at
    if not DATE_ARG_RE.match(at):
        print(f"--at must be YYYY-MM-DD, got {at!r}", file=sys.stderr)
        return 1
    act, corpus = resolve_act(args.act)
    if not act:
        print(f"no corpus act matches {args.act!r}", file=sys.stderr)
        return 1
    jb = act["jurabk"]
    title = act.get("long_title") or ""
    if corpus == "federal":
        try:
            history = official_history(jb)
            exact = exact_observed_state(
                history, at, store=OFFICIAL_STORE)
        except StateStoreError as exc:
            print(f"official state checkout failed integrity checks: {exc}",
                  file=sys.stderr)
            return 1
        if exact is not None:
            state, observation = exact
            observed_state = dict(state)
            observed_state.update(observation)
            try:
                result = render_markdown_snapshot(
                    state, requested_at=at, norm=getattr(args, "norm", None),
                    fallback_head=at, observed_state=observed_state)
            except ArchiveRequestError as exc:
                print(f"checkout failed: {exc}", file=sys.stderr)
                return 1
            # Markdown front matter carries the state hash, official URL,
            # source build and date basis, making stdout directly saveable.
            print(result["markdown"], end="")
            return 0

        print(f"== checkout {jb} @ {at} — {title[:42]}".rstrip())
        available = sorted({row["observed_at"]
                            for row in history["observations"]})
        print("   federal corpus · official public state store")
        print("\n  no exact official GII observation exists for this date; "
              "checkout will not silently substitute the nearest state.")
        if available:
            shown = ", ".join(available[-12:])
            prefix = "… " if len(available) > 12 else ""
            print(f"  exact observed date(s): {prefix}{shown}")
        else:
            print("  this act has no archived official observation yet")
        print("  observation dates prove retrieval only, not legal effect.")
        if not include_private_candidates():
            return 2

        vs = sorted((v for v in load("buzer", "versions.jsonl")
                     if v["jurabk"] == jb), key=lambda v: v["date"])
        print("\n   private fallback · buzer back-history "
              "(non-authoritative, 2006+)")
        if not vs:
            print("\n  no version history on file — GII HEAD is the "
                  "only known state:")
            print(f"  {(act.get('stand') or '')[:74]}")
            return 0
        cur = [v for v in vs if v["date"] <= at]
        after = [v for v in vs if v["date"] > at]
        print()
        if cur:
            v = cur[-1]
            _, amending = parse_buzer_title(v["title"])
            print(f"  ● in force @ {at}: consolidation of {v['date']}")
            print(f"              created by {amending[:62]}")
            print(f"              {v['synopsis_url']}")
        else:
            print(f"  ? {at} predates the back-history window — "
                  f"earliest known change is {vs[0]['date']};")
            print("              the text in force then is outside "
                  "snapshot coverage")
        if after:
            print(f"\n  {len(after)} further amendment(s) after {at} "
                  f"(latest {after[-1]['date']}) — NOT the current text")
            nxt = after[0]
            _, amending = parse_buzer_title(nxt["title"])
            print(f"  → next change {nxt['date']}: {amending[:52]}")
            print(f"              that diff is exactly what changed "
                  f"next:")
            print(f"              {nxt['synopsis_url']}")
        else:
            print(f"\n  no amendment after {at} on file — this is "
                  f"HEAD (GII: {(act.get('stand') or '')[:52]})")
    else:
        print(f"== checkout {jb} @ {at} — {title[:42]}".rstrip())
        evs = bavarian_events(jb)
        print("   bavarian corpus · amtlich BayRS ffn/XML "
              f"(BayRS {act.get('bayrs_nr')})")
        if not evs:
            print("\n  no amendment history on file — BAYERN.RECHT "
                  "HEAD is the only known state")
            return 0
        cur = [e for e in evs if e["date"] <= at]
        after = [e for e in evs if e["date"] > at]
        print()
        if cur:
            e = cur[-1]
            print(f"  ● in force @ {at}: consolidation of {e['date']}")
            print(f"              {(e['changed'] or e['act'])[:62]}")
            if e["act"] and e["changed"]:
                print(f"              by {e['act'][:60]}")
            if e["cite"]:
                print(f"              ({e['cite']})")
        else:
            print(f"  ? {at} predates the Fortführungsnachweis — "
                  f"earliest known change is {evs[0]['date']}; the "
                  f"Stammfassung was in force")
        if after:
            print(f"\n  {len(after)} further amendment(s) after {at} "
                  f"(latest {after[-1]['date']}) — NOT the current "
                  f"text")
            nxt = after[0]
            print(f"  → next change {nxt['date']}: "
                  f"{(nxt['changed'] or nxt['act'])[:48]}")
            if nxt["cite"]:
                print(f"              ({nxt['cite']}) — no synopsis; "
                      f"amtlich source is the GVBl itself")
        else:
            print(f"\n  no amendment after {at} on file — this is "
                  f"HEAD (BAYERN.RECHT build "
                  f"{(act.get('builddate') or '')[:10]})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="git blame / checkout over Lexgraph snapshots "
                    "(read-only, no network)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("blame",
                       help="amendments/patches touching one §/Art.")
    b.add_argument("act", help="jurabk (federal first, then Bavaria)")
    b.add_argument("ref", help='paragraph: "3a", "§ 3a", "Art. 4"')
    b.set_defaults(fn=cmd_blame)
    c = sub.add_parser("checkout",
                       help="which consolidated version was in force")
    c.add_argument("act", help="jurabk (federal first, then Bavaria)")
    c.add_argument("--at", required=True, metavar="YYYY-MM-DD",
                   help="point in time")
    c.add_argument("--norm", metavar="REF",
                   help='emit one exact observed norm, e.g. "§ 29a"')
    c.set_defaults(fn=cmd_checkout)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
