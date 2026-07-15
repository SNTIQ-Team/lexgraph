"""Lexgraph — temporal legislation and procedure API.

Serves the pre-built web data plane (`web/data/*.json`) as a small, honest
REST API. It does NOT recompute anything: `tools/build_web_data.py` is the
build step, these files ARE the data, and the endpoints below just project
them. Response shapes deliberately equal the JSON files' shapes — the web
visualizer and `docs/API.md` already rely on them.

Endpoints (see docs/API.md → "C) REST API"):

  GET /health                 liveness + data-plane check
  GET /version                dataset + built_at (from summary.json)
  GET /stats                  the summary.json counts
  GET /data-policy            public-build source exclusions and mode
  GET /feed?limit=            realtime event stream, newest first
  GET /acts                   the act index (wiki.json)
  GET /acts/{id}              one full act (acts/<id>.json); 404 if unknown
  GET /acts/{id}/archive      selectable HEAD + historical transition dates
  GET /acts/{id}/markdown     full act or one norm as dated Markdown
  GET /git?lane=&limit=       dated chronology (legacy route name), by lane
  GET /graph                  the QFS arena export (nodes/edges/beliefs/…)
  GET /hierarchy              competence-aware legal layers (EU/Bund/Länder)
  GET /eu-index               all in-force EU directives + basic regulations
  GET /procedures/watched     persistent DIP/EUR-Lex watch state + history
  GET /amendment-fates        reviewed amendment document-chain validations
  GET /search?q=              deep search + complete GII metadata discovery
  GET /decisions?q=&act=      court decisions (decisions.json), filterable
  GET /decisions/{id}         one decision; 404 if unknown
  GET /digest                 LLM digest of legislative activity; 404 if none

Data is loaded once at startup and cached in-process; the dataset is static
per deploy. Override the data directory with LEXGRAPH_DATA=/path/to/web/data.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from api.act_archive import (
    ArchiveRequestError,
    UnknownNormError,
    build_archive_index,
    markdown_filename,
    render_markdown_snapshot,
)
from api.gii_catalog import GiiCatalogIndex
from api.search_engine import SearchEngine, normalize_search_text
from api.procedure_search import search_procedures

# Deployment override: LEXGRAPH_DATA=/path/to/web/data (default: repo layout)
DATA_DIR = Path(os.environ.get(
    "LEXGRAPH_DATA",
    Path(__file__).resolve().parent.parent / "web" / "data"))

app = FastAPI(title="Lexgraph", version="1.1")

# git.json lane index → jurisdiction (0=EU, 1=Bund, 2=Bayern, 3=Länder)
LANES = ["EU", "Bund", "Bayern", "Länder"]

# in-process cache of the (static per deploy) data plane
_CACHE: dict[str, object] = {}
_SEARCH_ENGINE: SearchEngine | None = None
_SEARCH_LOCK = threading.RLock()
_GII_CATALOG_INDEX: GiiCatalogIndex | None = None
_GII_CATALOG_LOCK = threading.RLock()


def _load(name: str) -> object:
    """Read and cache one top-level web/data JSON file (e.g. 'summary')."""
    if name not in _CACHE:
        path = DATA_DIR / f"{name}.json"
        if not path.exists():
            raise HTTPException(
                503, f"data file missing: {path.name} — run "
                     "tools/build_web_data.py")
        with path.open(encoding="utf-8") as fh:
            _CACHE[name] = json.load(fh)
    return _CACHE[name]


def _cached(payload: object) -> JSONResponse:
    """The data plane is static per deploy, so let clients cache it too."""
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=3600"})


def _gii_catalog_index(rows: list[dict]) -> GiiCatalogIndex:
    """Return the process-wide normalized GII discovery index."""
    global _GII_CATALOG_INDEX
    with _GII_CATALOG_LOCK:
        if (_GII_CATALOG_INDEX is None
                or _GII_CATALOG_INDEX.source_rows is not rows):
            _GII_CATALOG_INDEX = GiiCatalogIndex(rows)
        return _GII_CATALOG_INDEX


def warm_search_indexes() -> None:
    """Pay immutable index setup costs during startup, not on first input."""
    try:
        payload = _load("gii_catalog")
    except HTTPException:
        return
    rows = payload.get("acts") if isinstance(payload, dict) else []
    if isinstance(rows, list):
        _gii_catalog_index(rows)


app.add_event_handler("startup", warm_search_indexes)


# --------------------------------------------------------------- service

@app.get("/health")
def health():
    """Liveness + data-plane check (used by uptime monitors)."""
    try:
        summary = _load("summary")
    except HTTPException as exc:
        raise HTTPException(503, exc.detail)
    return {"status": "ok", "built_at": summary.get("built_at"),
            "data_dir": str(DATA_DIR)}


@app.get("/version")
def version():
    """Build/version + built_at from summary.json."""
    summary = _load("summary")
    return {"dataset": "Lexgraph", "version": app.version,
            "built_at": summary.get("built_at"),
            "source": "https://github.com/SNTIQ-Team/lexgraph"}


@app.get("/stats")
def stats():
    """The summary.json counts (acts, patches, vorgaenge, EU, graph, …)."""
    return _load("summary")


@app.get("/data-policy")
def data_policy():
    """Machine-readable public-build/quarantine policy and exclusions."""
    return _cached(_load("data_policy"))


@app.get("/feed")
def feed(limit: int = Query(100, ge=1, le=600)):
    """The realtime event feed (feed.json), newest first, capped at `limit`."""
    events = _load("feed")
    return {"total": len(events), "limit": limit, "events": events[:limit]}


@app.get("/acts")
def acts():
    """The act index (wiki.json): id, jurabk, juris, title, norms, changes."""
    return _cached(_load("wiki"))


@app.get("/acts/{act_id}")
def act_detail(act_id: str):
    """One full act (acts/<id>.json): temporal, patches, versions, norms, …."""
    return _cached(_load_act(act_id))


def _load_act(act_id: str) -> dict:
    """Read one act safely; act files stay uncached to keep helpers simple."""
    # guard against path traversal — ids are flat slugs like fed_asylblg
    if "/" in act_id or "\\" in act_id or act_id.startswith("."):
        raise HTTPException(404, f"unknown act '{act_id}'")
    path = DATA_DIR / "acts" / f"{act_id}.json"
    if not path.exists():
        raise HTTPException(404, f"unknown act '{act_id}'")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _archive_head_fallback() -> object:
    """Deployment-wide source snapshot boundary from summary.json."""
    return _load("summary").get("built_at")


@app.get("/acts/{act_id}/archive")
def act_archive(act_id: str):
    """Selectable state dates, norm designators, and known coverage gaps.

    HEAD is the exact consolidated snapshot.  Earlier entries are conservative
    reconstructions and remain explicitly partial where old/new evidence is
    absent, truncated, empty-sided, or internally inconsistent.
    """
    try:
        payload = build_archive_index(
            _load_act(act_id), fallback_head=_archive_head_fallback())
    except ArchiveRequestError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _cached(payload)


@app.get("/acts/{act_id}/markdown", response_class=Response)
def act_markdown(
        act_id: str,
        at: str | None = Query(None, description="YYYY-MM-DD; omit for HEAD"),
        norm: str | None = Query(
            None, description="§/Art. designator; omit for the entire act"),
        download: bool = Query(False, description="send as a .md attachment"),
):
    """Full act or one §/Art. as raw ``text/markdown``.

    Response headers expose the resolved date and whether it is the exact HEAD
    snapshot.  Historical gaps are also embedded at the top of the Markdown.
    """
    try:
        result = render_markdown_snapshot(
            _load_act(act_id), requested_at=at, norm=norm,
            fallback_head=_archive_head_fallback())
    except UnknownNormError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ArchiveRequestError as exc:
        raise HTTPException(422, str(exc)) from exc

    headers = {
        "Cache-Control": "public, max-age=3600",
        "X-Lexgraph-Requested-Date": result["requested_at"],
        "X-Lexgraph-Resolved-Date": result["resolved_at"],
        "X-Lexgraph-Head-Date": result["head_date"],
        "X-Lexgraph-Exact": str(result["exact"]).lower(),
        "X-Lexgraph-Archive-Status": (
            "exact" if result["exact"] else "partial"),
        "X-Lexgraph-Missing-Transitions": str(len(result["gaps"])),
    }
    # Keep the raw Markdown response self-describing for browser clients.  Cap
    # this header to a compact summary; the Markdown body still lists every
    # selected-snapshot gap in full.
    header_gaps = list(result["gaps"][:8])
    if len(result["gaps"]) > len(header_gaps):
        header_gaps.append({
            "reason": "additional_gaps",
            "label": f"{len(result['gaps']) - len(header_gaps)} more gaps",
        })
    headers["X-Lexgraph-Archive-Gaps"] = json.dumps(
        header_gaps, ensure_ascii=True, separators=(",", ":"))
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{markdown_filename(result)}"')
    return Response(result["markdown"], media_type="text/markdown",
                    headers=headers)


@app.get("/git")
def git(lane: int | None = Query(None, ge=0, le=3),
        limit: int = Query(100, ge=1, le=1000)):
    """Dated legal chronology, optionally filtered by lane, newest first.

    ``/git`` and the ``commits`` key are legacy compatibility names.  Lane:
    0=EU, 1=Bund, 2=Bayern, 3=Länder.
    """
    data = _load("git")
    commits = data["commits"]
    if lane is not None:
        commits = [c for c in commits if c.get("lane") == lane]
    return {"lanes": data.get("lanes", LANES),
            "lane": lane, "total": len(commits),
            "commits": commits[:limit]}


@app.get("/graph")
def graph():
    """The QFS arena export (graph.json): nodes/edges/beliefs/ticks/worlds."""
    return _cached(_load("graph"))


@app.get("/hierarchy")
def hierarchy():
    """Competence-aware legal layers (hierarchy.json), not a total ranking."""
    return _cached(_load("hierarchy"))


@app.get("/eu-index")
def eu_index(q: str | None = Query(None, min_length=1),
             kind: str | None = Query(None, pattern="^(DIR|REG)$"),
             limit: int = Query(100, ge=1, le=500),
             offset: int = Query(0, ge=0)):
    """All in-force directives (incl. delegated/implementing) and all basic
    regulations as metadata — CELEX, type, date, title. Search plus offset/
    limit pagination keeps responses small while leaving the full ~8k-row
    index enumerable. 404 until the index has been fetched once.
    """
    path = DATA_DIR / "eu_index.json"
    if not path.exists():
        raise HTTPException(404, "eu index not built yet")
    data = _load("eu_index")
    rows = data["instruments"]
    if kind:
        rows = [r for r in rows if r["type"].startswith(kind)]
    if q:
        needle = q.strip().casefold()
        rows = [r for r in rows
                if needle in r["celex"].casefold()
                or needle in r["title"].casefold()]
    return {"built_at": data["built_at"], "total": data["total"],
            "matched": len(rows), "offset": offset, "limit": limit,
            "instruments": rows[offset:offset + limit]}


@app.get("/procedures/watched")
def watched_procedures():
    """Persistent tracked procedures, including immutable terminal history.

    DIP and EUR-Lex stages are reported as observed.  In particular, an EU
    political agreement remains active. Even OJ publication stays active as
    ``pending_final_review`` until the final Article 2 has been compared with
    the tracked proposal and that review is persisted.
    """
    return _cached(_load("watched_procedures"))


@app.get("/amendment-fates")
def amendment_fates(
        procedure_id: str | None = Query(
            None, description="official DIP procedure id"),
        validation_id: str | None = Query(
            None, description="Lexgraph validation record id"),
):
    """Reviewed parliamentary document chains and current-law checks."""
    data = _load("amendment_fates")
    if not procedure_id and not validation_id:
        return _cached(data)
    rows = data.get("records") or []
    if procedure_id:
        rows = [row for row in rows
                if str(row.get("procedure_id")) == procedure_id]
    if validation_id:
        rows = [row for row in rows
                if str(row.get("id")) == validation_id]
    return _cached({
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "total": len(rows),
        "validated": sum(bool((row.get("validation") or {}).get("passed"))
                         for row in rows),
        "records": rows,
    })


def _append_gii_catalog(result: dict, query: str, limit: int) -> dict:
    """Append metadata-only breadth matches after deep corpus results."""
    try:
        payload = _load("gii_catalog")
    except HTTPException:
        payload = {"acts": []}  # compatibility with older data deployments
    rows = payload.get("acts") if isinstance(payload, dict) else []
    deep_act_ids = {
        str(row.get("act_id") or row.get("id") or "")
        for row in result.get("act_matches") or []
    }
    deep_act_ids.update(
        str(row.get("act_id") or "")
        for row in result.get("norm_matches") or [])
    # The final section is breadth discovery, not a second rendering of the
    # curated corpus.  Exclude every catalogue row that already has a local
    # deep act, including deep hits beyond the current result-page limit.
    deep_act_ids.update(str(row.get("act_id")) for row in rows or []
                        if row.get("act_id"))
    matches, total = _gii_catalog_index(rows or []).search(
        query, limit=limit, exclude_act_ids=deep_act_ids)
    result["catalog_total"] = total
    result["catalog_matches"] = matches
    result["result_total"] = int(result.get("result_total") or 0) + total
    return result


@app.get("/search")
def search(q: str = Query(..., min_length=1),
           limit: int = Query(25, ge=1, le=200),
           norm_limit: int = Query(50, ge=1, le=200),
           procedure_limit: int = Query(20, ge=1, le=100),
           catalog_limit: int = Query(25, ge=1, le=100)):
    """Ranked Unicode full-text search over acts and current norms.

    ``matches`` and ``total`` retain the original act-search contract.
    Enriched act results live in ``act_matches``; norm results include their
    act, §/Art., a plain-text snippet, provenance, score, and detail link.
    Synonyms (including multilingual Ukraine/temporary-protection terms) are
    data-driven and embedded into the built ``search.sqlite`` artifact.

    If an old deployment has no FTS artifact yet, the endpoint degrades to the
    previous title/abbreviation substring search instead of failing.  Complete
    GII catalogue matches are appended as metadata-only discovery results and
    never masquerade as locally indexed full text.
    """
    rows = _load("wiki")
    # DIP has only a few hundred current procedures.  Count the full match set
    # first so ``procedure_total`` follows the act/norm total contract, then
    # expose only the requested page.
    all_procedure_matches = search_procedures(
        _load("hierarchy"), q, 10_000)
    procedure_total = len(all_procedure_matches)
    procedure_matches = all_procedure_matches[:procedure_limit]
    index_path = DATA_DIR / "search.sqlite"
    if not index_path.is_file():
        needle = normalize_search_text(q)
        all_matches = [r for r in rows
                       if needle in normalize_search_text(r.get("id"))
                       or needle in normalize_search_text(r.get("jurabk"))
                       or needle in normalize_search_text(r.get("title"))]
        matches = all_matches[:limit]
        return _append_gii_catalog({
            "query": q, "total": len(all_matches), "matches": matches,
            "result_total": len(all_matches) + procedure_total,
            "act_total": len(all_matches),
            "norm_total": 0, "act_matches": matches,
            "norm_matches": [],
            "procedure_total": procedure_total,
            "procedure_matches": procedure_matches,
        }, q, catalog_limit)

    global _SEARCH_ENGINE
    with _SEARCH_LOCK:
        if _SEARCH_ENGINE is None or _SEARCH_ENGINE.path != index_path:
            if _SEARCH_ENGINE is not None:
                _SEARCH_ENGINE.close()
            _SEARCH_ENGINE = SearchEngine(index_path, rows)
        # There are only dozens of curated acts.  Ask the engine for all act
        # candidates so the legacy substring union below has an exact count,
        # then apply the public limit after de-duplication.
        result = _SEARCH_ENGINE.search(q, act_limit=max(limit, len(rows)),
                                       norm_limit=norm_limit)

    # Preserve the old arbitrary-substring behavior for act abbreviations
    # (FTS prefix matching cannot find a query in the middle of one token).
    needle = normalize_search_text(q)
    legacy = [r for r in rows
              if needle and (needle in normalize_search_text(r.get("id"))
                             or needle in normalize_search_text(
                                 r.get("jurabk"))
                             or needle in normalize_search_text(
                                 r.get("title")))]
    seen = {r["id"] for r in result["act_matches"]}
    for row in legacy:
        if row["id"] in seen:
            continue
        enriched = dict(row)
        enriched.update({
            "score": 0.0,
            "snippet": row.get("title") or row.get("jurabk") or "",
            "matched_fields": ["legacy_substring"],
            "source": "gii" if row.get("juris") == "DE"
            else "bayern_recht",
            "url": f"/acts/{row['id']}",
        })
        result["act_matches"].append(enriched)
        seen.add(row["id"])
    result["act_matches"] = result["act_matches"][:limit]
    result["matches"] = [
        {key: value for key, value in row.items()
         if key not in {"score", "snippet", "matched_fields", "source",
                        "url"}}
        for row in result["act_matches"]]
    result["act_total"] = len(seen)
    result["total"] = len(seen)
    result["procedure_total"] = procedure_total
    result["procedure_matches"] = procedure_matches
    result["result_total"] = (result["act_total"] + result["norm_total"]
                              + result["procedure_total"])
    return _append_gii_catalog(result, q, catalog_limit)


@app.get("/decisions")
def decisions(q: str | None = Query(None, min_length=1),
              act: str | None = Query(None, min_length=1),
              limit: int = Query(50, ge=1, le=200)):
    """Court decisions (decisions.json), newest first, optionally filtered.

    `q` matches case-insensitively in az, court, court_short, title, every
    summary language, and effects[].jurabk; `act` filters by effects[].act_id
    (an act index id like fed_asylblg).
    """
    rows = _load("decisions")
    if act:
        rows = [d for d in rows
                if any(e.get("act_id") == act
                       for e in d.get("effects") or [])]
    if q:
        needle = q.strip().casefold()

        def hit(d: dict) -> bool:
            hay = [d.get("az"), d.get("court"), d.get("court_short"),
                   d.get("title"),
                   *(d.get("summary") or {}).values(),
                   *(e.get("jurabk") for e in d.get("effects") or [])]
            return any(needle in str(h).casefold() for h in hay if h)

        rows = [d for d in rows if hit(d)]
    return {"query": q, "act": act, "total": len(rows),
            "decisions": rows[:limit]}


@app.get("/decisions/{decision_id}")
def decision_detail(decision_id: str):
    """One exported decision row by id; 404 if unknown."""
    for d in _load("decisions"):
        if d.get("id") == decision_id:
            return _cached(d)
    raise HTTPException(404, f"unknown decision '{decision_id}'")


@app.get("/digest")
def digest():
    """The LLM digest (digest.json): {generated_at, model, llm, periods}.

    Read fresh per request, NOT via _load(): the file is rewritten by every
    refresh (tools/build_digest.py) and is legitimately absent when no
    OPENROUTER_API_KEY is configured — that must stay a 404, not a
    permanently cached copy or a 503. The file is tiny, so no cache needed.
    """
    path = DATA_DIR / "digest.json"
    if not path.exists():
        raise HTTPException(404, "no digest available")
    with path.open(encoding="utf-8") as fh:
        return _cached(json.load(fh))
