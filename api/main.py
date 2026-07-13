"""Lexgraph — legislation API (event-sourced git over German law).

Serves the pre-built web data plane (`web/data/*.json`) as a small, honest
REST API. It does NOT recompute anything: `tools/build_web_data.py` is the
build step, these files ARE the data, and the endpoints below just project
them. Response shapes deliberately equal the JSON files' shapes — the web
visualizer and `docs/API.md` already rely on them.

Endpoints (see docs/API.md → "C) REST API"):

  GET /health                 liveness + data-plane check
  GET /version                dataset + built_at (from summary.json)
  GET /stats                  the summary.json counts
  GET /feed?limit=            realtime event stream, newest first
  GET /acts                   the act index (wiki.json)
  GET /acts/{id}              one full act (acts/<id>.json); 404 if unknown
  GET /git?lane=&limit=       the commit-graph, optionally filtered by lane
  GET /graph                  the QFS arena export (nodes/edges/beliefs/…)
  GET /hierarchy              the jurisdiction tree (eu/bund/bayern/laender)
  GET /search?q=              search acts by jurabk/title (from wiki.json)
  GET /decisions?q=&act=      court decisions (decisions.json), filterable
  GET /decisions/{id}         one decision; 404 if unknown
  GET /digest                 LLM digest of legislative activity; 404 if none

Data is loaded once at startup and cached in-process; the dataset is static
per deploy. Override the data directory with LEXGRAPH_DATA=/path/to/web/data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# Deployment override: LEXGRAPH_DATA=/path/to/web/data (default: repo layout)
DATA_DIR = Path(os.environ.get(
    "LEXGRAPH_DATA",
    Path(__file__).resolve().parent.parent / "web" / "data"))

app = FastAPI(title="Lexgraph", version="1.0")

# git.json lane index → jurisdiction (0=EU, 1=Bund, 2=Bayern, 3=Länder)
LANES = ["EU", "Bund", "Bayern", "Länder"]

# in-process cache of the (static per deploy) data plane
_CACHE: dict[str, object] = {}


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
    # guard against path traversal — ids are flat slugs like fed_asylblg
    if "/" in act_id or "\\" in act_id or act_id.startswith("."):
        raise HTTPException(404, f"unknown act '{act_id}'")
    path = DATA_DIR / "acts" / f"{act_id}.json"
    if not path.exists():
        raise HTTPException(404, f"unknown act '{act_id}'")
    with path.open(encoding="utf-8") as fh:
        return _cached(json.load(fh))


@app.get("/git")
def git(lane: int | None = Query(None, ge=0, le=3),
        limit: int = Query(100, ge=1, le=1000)):
    """The commit-graph (git.json), optionally filtered by lane, newest first.

    lane: 0=EU, 1=Bund, 2=Bayern, 3=Länder (git.json's integer lane index).
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
    """The jurisdiction tree (hierarchy.json): eu / bund / bayern / laender."""
    return _cached(_load("hierarchy"))


@app.get("/search")
def search(q: str = Query(..., min_length=1),
           limit: int = Query(25, ge=1, le=200)):
    """Search acts by jurabk or title (substring, case-insensitive).

    Returns matching rows from the act index (wiki.json), so the shape equals
    an `/acts` slice — clients can render the same list.
    """
    needle = q.strip().casefold()
    rows = _load("wiki")
    matches = [r for r in rows
               if needle in str(r.get("jurabk", "")).casefold()
               or needle in str(r.get("title", "")).casefold()]
    return {"query": q, "total": len(matches), "matches": matches[:limit]}


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
    """One decision by id (a decisions.json row, incl. anonymized full text)."""
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
