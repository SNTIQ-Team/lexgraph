"""European Parliament pipeline detail via the EP Open Data API.

Base https://data.europarl.europa.eu/api/v2/ (verified 2026-07-06):
GET on the base path returns the OpenAPI 3.0.3 spec as JSON; no auth;
CC BY 4.0; ~500 req/5min; JSON-LD via format=application/ld+json.
PITFALL: unfiltered list endpoints TIME OUT — always pass a filter
(year=...) and a limit.

Verified endpoints:
    /adopted-texts?year=YYYY&language=en&offset=N&limit=100
        -> {data: [FRBR Work], meta: {total}}. language=en keeps
        meta.total identical but trims the is_realized_by expression
        tree and title_dcterms to EN — 14x smaller pages (10 KB vs
        138 KB per 3 items). Work carries: identifier, document_date,
        is_about (EuroVoc concept URIs, NO labels inline), and the
        procedure link as eli/dl/proc/YYYY-NNNN inside
        inverse_created_a_realization_of.
    /procedures?year=YYYY&offset=N&limit=M
        The OpenAPI spec documents only process-type/format/offset/
        limit on /procedures, but year= WORKS (undocumented,
        verified: returns only YYYY-* ids; 2024->655, 2025->725,
        2026->105). parliamentary-term= is silently IGNORED (same
        rows as the unfiltered list). List items carry process_id,
        process_type and the OEIL-style label ("2024/0003(BUD)")
        only — no title/status/dates; those need the detail call.
    /procedures/{process-id}          (e.g. 2022-0066)
        -> process_title (multilingual), current_stage
        (procedure-phase URI), consists_of[].activity_date.
    https://publications.europa.eu/webapi/rdf/sparql
        EuroVoc labels are NOT inline in the API responses and the
        EP /controlled-vocabularies endpoint only lists 5 EP-internal
        vocabularies (no EuroVoc), so EN prefLabels are batch-
        resolved here with a VALUES query (100 URIs/request).

Output (data/snapshots/ep_layer/<date>/):
    adopted_texts.jsonl {id, eli, title, date, eurovoc[],
                         procedure_ref, relevant}
    procedures.jsonl    {process_id, oeil_ref, process_type, title,
                         status, dates} for 2024-2026
    ep_events.jsonl     one LexEvent per adopted text

Usage:
    python3 pipeline/fetch_ep_procedures.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

from common import Http, snapshot_dir, write_jsonl

API = "https://data.europarl.europa.eu/api/v2"
LD_JSON = "application/ld+json"
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
EUROVOC = "http://eurovoc.europa.eu/"

TEXT_YEARS = (2025, 2026)
PROC_YEARS = (2024, 2025, 2026)
PAGE = 100                    # verified: full pages, EN titles on all
LIST_LIMIT = 1000             # /procedures per-year lists fit in one

RELEVANT_RE = re.compile(
    r"(?i)asylum|migration|refugee|schengen|visa|eurodac|frontex|"
    r"return|reception|resettlement")


def as_list(v) -> list:
    """JSON-LD single-vs-array pitfall: fields like process_type or
    label arrive as a string on most rows but as a LIST on some
    (first seen: 2018 procedures) — normalize."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def uri_tail(v) -> str | None:
    """Last path segment; multi-valued fields joined with '+'."""
    tails = [str(x).rsplit("/", 1)[-1] for x in as_list(v) if x]
    return "+".join(tails) or None


def api_json(http: Http, path: str, **params) -> dict:
    params.setdefault("format", LD_JSON)
    r = http.get(f"{API}{path}", params=params, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} on {path}")
    return r.json()


def procedures_list_filter(http: Http) -> bool:
    """Step 1: what does the spec say about /procedures, and does a
    year filter actually work? The spec only documents process-type,
    so the year= support is probed empirically (2 rows)."""
    try:
        spec = http.get(f"{API}/", timeout=60,
                        headers={"Accept": "application/json"}).json()
        raw = spec["paths"]["/procedures"]["get"].get("parameters", [])
        names = []
        for p in raw:
            if "$ref" in p:
                p = spec["components"]["parameters"][
                    p["$ref"].rsplit("/", 1)[-1]]
            if p.get("in") == "query":
                names.append(p["name"])
        print(f"[spec] /procedures documented query params: {names}")
    except Exception as exc:                # noqa: BLE001 — non-fatal
        print(f"[spec] FAILED to read OpenAPI spec: {exc}")
    try:
        probe = api_json(http, "/procedures", year="2024", limit="2")
        ids = [w.get("process_id", "") for w in probe.get("data", [])]
        ok = bool(ids) and all(i.startswith("2024-") for i in ids)
        print(f"[spec] empirical year=2024 probe -> {ids} "
              f"(filter {'WORKS' if ok else 'IGNORED'})")
        return ok
    except Exception as exc:                # noqa: BLE001
        print(f"[spec] year-filter probe FAILED: {exc}")
        return False


def harvest_texts(http: Http, year: int) -> tuple[list[dict], int]:
    """All adopted-text Works for one year, paginated."""
    works: list[dict] = []
    failed = 0
    offset, total = 0, None
    while True:
        try:
            d = api_json(http, "/adopted-texts", year=str(year),
                         language="en", offset=str(offset),
                         limit=str(PAGE))
        except Exception as exc:            # noqa: BLE001 — keep going
            print(f"[texts {year} offset {offset}] FAILED: {exc}")
            failed += 1
            if total is None:               # can't page a blind total
                return works, failed
            offset += PAGE
            if offset >= total:
                return works, failed
            continue
        if total is None:
            total = int(d.get("meta", {}).get("total") or 0)
            print(f"[texts {year}] site reports {total} adopted texts")
        data = d.get("data", [])
        works.extend(data)
        offset += PAGE
        if not data or offset >= total:
            break
    if total is not None and len(works) + failed * PAGE < total:
        print(f"[warn] texts {year}: got {len(works)} of {total}")
    return works, failed


def resolve_eurovoc(http: Http, uris: set[str]) -> tuple[dict, int]:
    """uri -> EN prefLabel via one VALUES query per 100 concepts."""
    labels: dict[str, str] = {}
    failed = 0
    todo = sorted(uris)
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        q = ("PREFIX skos: <http://www.w3.org/2004/02/skos/core#> "
             "SELECT ?c ?l WHERE { VALUES ?c { "
             + " ".join(f"<{u}>" for u in chunk)
             + " } ?c skos:prefLabel ?l FILTER(lang(?l)='en') }")
        try:
            r = http.get(SPARQL, timeout=90, params={
                "query": q, "format": "application/sparql-results+json"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            for b in r.json()["results"]["bindings"]:
                labels[b["c"]["value"]] = b["l"]["value"]
        except Exception as exc:            # noqa: BLE001 — keep going
            print(f"[eurovoc chunk {i // 100}] FAILED: {exc}")
            failed += 1
    return labels, failed


def proc_year_list(http: Http, year: int) -> tuple[dict, int]:
    """process_id -> {label, process_type} for one year (paged only
    if a year ever outgrows LIST_LIMIT)."""
    out: dict[str, dict] = {}
    failed = 0
    offset = 0
    while True:
        try:
            d = api_json(http, "/procedures", year=str(year),
                         offset=str(offset), limit=str(LIST_LIMIT))
        except Exception as exc:            # noqa: BLE001
            print(f"[procs {year} offset {offset}] FAILED: {exc}")
            return out, failed + 1
        data = d.get("data", [])
        for it in data:
            pids = as_list(it.get("process_id"))
            if pids:
                labels = as_list(it.get("label"))
                out[str(pids[0])] = {
                    "label": str(labels[0]) if labels else None,
                    "process_type": uri_tail(it.get("process_type"))}
        if len(data) < LIST_LIMIT:
            return out, failed
        offset += LIST_LIMIT


def pick_title(work: dict) -> str | None:
    t = work.get("title_dcterms")
    if not isinstance(t, dict):
        t = {}
    if "en" in t:
        return t["en"]
    for lang in sorted(t):                  # original-language fallback
        return t[lang]
    return None


def text_rows(works: list[dict], labels: dict[str, str],
              proc_map: dict[str, dict],
              fetched_at: str) -> tuple[list[dict], list[dict], int]:
    """(adopted_texts rows, ep_events rows, multi-procedure count)."""
    texts: list[dict] = []
    events: list[dict] = []
    multi_proc = 0
    for w in works:
        idents = as_list(w.get("identifier"))
        ident = (str(idents[0]) if idents
                 else str(w["id"]).rsplit("/", 1)[-1])
        title = pick_title(w)
        eurovoc = [{"id": u.rsplit("/", 1)[-1], "label": labels.get(u)}
                   for u in as_list(w.get("is_about"))
                   if isinstance(u, str) and u.startswith(EUROVOC)]
        pids = [x.rsplit("/", 1)[-1]
                for x in as_list(
                    w.get("inverse_created_a_realization_of"))
                if isinstance(x, str) and "/proc/" in x]
        if len(pids) > 1:
            multi_proc += 1
        ref = None
        if pids:
            pid = pids[0]
            known = proc_map.get(pid, {}).get("label")
            ref = known or f"{pid[:4]}/{pid[5:]}"   # OEIL sans type
        hay = " ".join([title or ""]
                       + [e["label"] for e in eurovoc if e["label"]])
        relevant = bool(RELEVANT_RE.search(hay))
        texts.append({
            "id": ident,
            "eli": f"https://data.europarl.europa.eu/{w['id']}",
            "title": title,
            "date": w.get("document_date"),
            "eurovoc": eurovoc,
            "procedure_ref": ref,
            "relevant": relevant})
        events.append({
            "event_id": f"ep_{ident}",
            "kind": "adopted",
            "jurisdiction": "EU",
            "actor": "European Parliament",
            "time": w.get("document_date"),
            "title": title,
            "relevant": relevant,
            "source": "ep_opendata",
            "fetched_at": fetched_at})
    return texts, events, multi_proc


def proc_rows(http: Http, proc_map: dict[str, dict]) -> tuple[list, int]:
    """Detail-fetch every 2024-2026 procedure for title/status/dates;
    a failed detail keeps the list-level row (title/status None)."""
    pids = sorted(p for p in proc_map
                  if p[:4].isdigit() and int(p[:4]) in PROC_YEARS)
    rows: list[dict] = []
    failed = 0
    for i, pid in enumerate(pids, 1):
        row = {"process_id": pid,
               "oeil_ref": proc_map[pid].get("label"),
               "process_type": proc_map[pid].get("process_type"),
               "title": None, "status": None,
               "dates": {"first_activity": None, "last_activity": None}}
        try:
            d = api_json(http, f"/procedures/{pid}")
            data = d.get("data") or []
            it = data[0] if isinstance(data, list) else data
            row["title"] = pick_title(
                {"title_dcterms": it.get("process_title")})
            row["status"] = uri_tail(it.get("current_stage"))
            dates = sorted(str(a["activity_date"])
                           for a in as_list(it.get("consists_of"))
                           if isinstance(a, dict)
                           and a.get("activity_date"))
            if dates:
                row["dates"] = {"first_activity": dates[0],
                                "last_activity": dates[-1]}
        except Exception as exc:            # noqa: BLE001 — keep going
            print(f"[proc detail {pid}] FAILED: {exc}")
            failed += 1
        rows.append(row)
        if i % 100 == 0 or i == len(pids):
            print(f"[proc detail] {i}/{len(pids)} "
                  f"({failed} failed)", flush=True)
    return rows, failed


def main() -> int:
    http = Http(delay=1.0)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    listable = procedures_list_filter(http)

    works: list[dict] = []
    text_fail = 0
    for year in TEXT_YEARS:
        w, f = harvest_texts(http, year)
        print(f"[texts {year}] fetched {len(w)} works, {f} failed pages")
        works, text_fail = works + w, text_fail + f
    if not works and text_fail:
        print("[err] zero adopted texts with failures — refusing to "
              "overwrite the snapshot with an empty harvest")
        return 1

    uris = {u for w in works for u in as_list(w.get("is_about"))
            if isinstance(u, str) and u.startswith(EUROVOC)}
    labels, ev_fail = resolve_eurovoc(http, uris)
    print(f"[eurovoc] {len(labels)}/{len(uris)} concepts labelled, "
          f"{ev_fail} failed chunks")

    proc_map: dict[str, dict] = {}
    list_fail = 0
    if listable:
        ref_years = {int(x.rsplit("/", 1)[-1][:4])
                     for w in works
                     for x in as_list(
                         w.get("inverse_created_a_realization_of"))
                     if isinstance(x, str) and "/proc/" in x
                     and x.rsplit("/", 1)[-1][:4].isdigit()}
        for year in sorted(set(PROC_YEARS) | ref_years):
            m, f = proc_year_list(http, year)
            print(f"[procs {year}] {len(m)} in list, {f} failures")
            proc_map.update(m)
            list_fail += f

    texts, events, multi_proc = text_rows(works, labels, proc_map,
                                          fetched_at)

    procedures: list[dict] = []
    detail_fail = 0
    if listable:
        procedures, detail_fail = proc_rows(http, proc_map)

    out = snapshot_dir("ep_layer")
    n_texts = write_jsonl(out / "adopted_texts.jsonl", texts)
    n_events = write_jsonl(out / "ep_events.jsonl", events)
    if listable and (procedures or not (list_fail or detail_fail)):
        n_procs = write_jsonl(out / "procedures.jsonl", procedures)
    else:
        n_procs = 0
        print("[warn] procedures.jsonl not written "
              f"(listable={listable}, {list_fail} list failures)")

    relevant = sum(1 for t in texts if t["relevant"])
    unlabelled = sum(1 for t in texts
                     for e in t["eurovoc"] if not e["label"])
    no_ref = sum(1 for t in texts if not t["procedure_ref"])
    failures = text_fail + ev_fail + list_fail + detail_fail
    print(f"\nfetched {n_texts} adopted texts "
          f"({relevant} migration/asylum-relevant), {n_events} events, "
          f"{n_procs} procedures -> {out}")
    print(f"gaps: {text_fail} failed text pages, {ev_fail} failed "
          f"EuroVoc chunks ({unlabelled} concept refs unlabelled), "
          f"{list_fail} failed procedure lists, {detail_fail} failed "
          f"procedure details, {no_ref} texts without procedure ref, "
          f"{multi_proc} texts with >1 procedure (first kept)")
    if failures:
        print(f"[warn] partial harvest: {failures} failures — "
              "snapshot written with what we got")
    return 0


if __name__ == "__main__":
    sys.exit(main())
