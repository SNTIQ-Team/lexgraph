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
  GET /acts/{id}/history      bitemporal legal/knowledge-time assertions
  GET /acts/{id}/diff         exact state diff at one knowledge-time slice
  GET /acts/{id}/markdown     full act or one norm as dated Markdown
  GET /retrospective-history.sqlite  portable retrospective database
  GET /git?lane=&limit=       Laws-as-Git event log, optionally by lane
  GET /graph                  the QFS arena export (nodes/edges/beliefs/…)
  GET /hierarchy              competence-aware legal layers (EU/Bund/Länder)
  GET /eu-index               all in-force EU directives + basic regulations
  GET /procedures/watched     persistent DIP/EUR-Lex watch state + history
  GET /amendment-fates        reviewed amendment document-chain validations
  GET /federal-history        official-only verified federal state/patch events
  GET /official-states        exact GII retrieval observations + state hashes
  GET /official-transition-reviews  BGBl/DIP accepted legal transitions
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
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from api.act_archive import (
    ArchiveRequestError,
    UnknownNormError,
    build_archive_index,
    markdown_filename,
    render_markdown_snapshot,
)
from api.gii_catalog import GiiCatalogIndex
from api.search_engine import SearchEngine, normalize_search_text
from api.procedure_search import ProcedureSearchIndex
from api.official_state_store import OfficialStateError, load_observed_state
from api.retrospective_store import (
    RetrospectiveAmbiguity,
    RetrospectiveIntegrityError,
    RetrospectiveNotFound,
    act_history as resolve_act_history,
    diff_intervals,
    load_interval_state,
    resolve_interval,
    validate_manifest as validate_retrospective_manifest,
)

# Deployment override: LEXGRAPH_DATA=/path/to/web/data (default: repo layout)
DATA_DIR = Path(os.environ.get(
    "LEXGRAPH_DATA",
    Path(__file__).resolve().parent.parent / "web" / "data"))

app = FastAPI(title="Lexgraph", version="1.3")

# git.json lane index → jurisdiction (0=EU, 1=Bund, 2=Bayern, 3=Länder)
LANES = ["EU", "Bund", "Bayern", "Länder"]

# in-process cache of the (static per deploy) data plane
_CACHE: dict[str, object] = {}
_SEARCH_ENGINE: SearchEngine | None = None
_SEARCH_LOCK = threading.RLock()
_GII_CATALOG_INDEX: GiiCatalogIndex | None = None
_GII_CATALOG_LOCK = threading.RLock()
_PROCEDURE_SEARCH_INDEX: ProcedureSearchIndex | None = None
_PROCEDURE_SEARCH_LOCK = threading.RLock()
_RETROSPECTIVE_MANIFEST: dict[str, object] | None = None
_RETROSPECTIVE_SOURCE: object | None = None
_RETROSPECTIVE_LOCK = threading.RLock()


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


def _retrospective_manifest(*, optional: bool = False) -> dict | None:
    """Return the validated, immutable retrospective data-plane manifest."""
    path = DATA_DIR / "retrospective_history.json"
    if not path.is_file():
        if optional:
            return None
        raise HTTPException(
            503, "retrospective history is not built — run "
                 "tools/build_web_data.py")
    raw = _load("retrospective_history")
    global _RETROSPECTIVE_MANIFEST, _RETROSPECTIVE_SOURCE
    with _RETROSPECTIVE_LOCK:
        if _RETROSPECTIVE_MANIFEST is None or _RETROSPECTIVE_SOURCE is not raw:
            try:
                _RETROSPECTIVE_MANIFEST = validate_retrospective_manifest(raw)
            except RetrospectiveIntegrityError as exc:
                raise HTTPException(
                    503, f"retrospective history integrity failure: {exc}") \
                    from exc
            _RETROSPECTIVE_SOURCE = raw
        return _RETROSPECTIVE_MANIFEST


def _knowledge_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(422, "as_of must include a timezone")
    return value.isoformat()


def _history_envelope(manifest: dict, history: dict) -> dict:
    """Attach manifest-level semantics without duplicating the state catalog."""
    return {
        "schema_version": manifest.get("schema_version"),
        "kind": manifest.get("kind"),
        "built_at": manifest.get("built_at"),
        "state_identity": manifest.get("state_identity"),
        "date_semantics": manifest.get("date_semantics"),
        "source_policy": manifest.get("source_policy"),
        **history,
    }


def _gii_catalog_index(rows: list[dict]) -> GiiCatalogIndex:
    """Return the process-wide normalized GII discovery index."""
    global _GII_CATALOG_INDEX
    with _GII_CATALOG_LOCK:
        if (_GII_CATALOG_INDEX is None
                or _GII_CATALOG_INDEX.source_rows is not rows):
            _GII_CATALOG_INDEX = GiiCatalogIndex(rows)
        return _GII_CATALOG_INDEX


def _procedure_search_index(hierarchy: object) -> ProcedureSearchIndex:
    global _PROCEDURE_SEARCH_INDEX
    with _PROCEDURE_SEARCH_LOCK:
        if (_PROCEDURE_SEARCH_INDEX is None
                or _PROCEDURE_SEARCH_INDEX.source_hierarchy is not hierarchy):
            _PROCEDURE_SEARCH_INDEX = ProcedureSearchIndex(hierarchy)
        return _PROCEDURE_SEARCH_INDEX


def warm_search_indexes() -> None:
    """Pay immutable index setup costs during startup, not on first input."""
    try:
        payload = _load("gii_catalog")
    except HTTPException:
        return
    rows = payload.get("acts") if isinstance(payload, dict) else []
    if isinstance(rows, list):
        _gii_catalog_index(rows)
    try:
        hierarchy = _load("hierarchy")
    except HTTPException:
        return
    _procedure_search_index(hierarchy)


# Uvicorn imports this module once per worker.  Warming here is compatible
# with both old and current FastAPI versions (the latter removed the former
# ``add_event_handler`` convenience method) and keeps the first user query
# fast without adding a framework-specific lifecycle dependency.
warm_search_indexes()


# --------------------------------------------------------------- service

@app.get("/health")
def health():
    """Liveness + validated data-plane check (used by deploys/monitors).

    The atomic publisher treats this endpoint as its commit gate.  Validate
    the retrospective manifest here as well as ``summary.json`` so a release
    with internally inconsistent bitemporal assertions is rolled back before
    it becomes visible, rather than failing on the first history request.
    """
    try:
        summary = _load("summary")
    except HTTPException as exc:
        raise HTTPException(503, exc.detail)
    retrospective = _retrospective_manifest(optional=True)
    return {"status": "ok", "built_at": summary.get("built_at"),
            "data_dir": str(DATA_DIR),
            "retrospective": ({
                "status": "ok",
                "built_at": retrospective.get("built_at"),
                "counts": retrospective.get("counts", {}),
            } if retrospective is not None else {"status": "not_built"})}


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


@app.get("/federal-history")
def federal_history(
        act: str | None = Query(None, min_length=1),
        tier: str | None = Query(
            None, pattern="^(exact|current_text_correspondence|metadata_only)$"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Evidence-bound federal history built only from official GII/BGBl/DIP.

    Buzer is not an input to this public file. ``exact`` means two captured
    official GII states; it does not silently infer their legal effective day.
    """
    data = _load("verified_federal_events")
    events = data.get("events") or []
    if act:
        needle = act.casefold().strip()
        events = [row for row in events
                  if str(row.get("act") or "").casefold() == needle]
    if tier:
        events = [row for row in events
                  if row.get("verification") == tier]
    return {
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "source_policy": data.get("source_policy"),
        "tiers": data.get("tiers"),
        "total": data.get("total"),
        "matched": len(events),
        "offset": offset,
        "limit": limit,
        "events": events[offset:offset + limit],
    }


@app.get("/official-states")
def official_states(
        act: str | None = Query(
            None, min_length=1, description="act id or exact jurabk"),
        limit: int = Query(250, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Immutable complete GII states indexed by exact retrieval date.

    The observations are evidence that GII served a complete parsed state on
    that day, not a claim about when its amendments entered into force.  The
    full body is retrieved through ``/acts/{id}/markdown?at=...`` so the same
    integrity-checked path serves both whole acts and individual norms.
    """
    data = _load("official_federal_states")
    rows = list(data.get("observations") or [])
    if act:
        needle = act.casefold().strip()
        rows = [row for row in rows if needle in {
            str(row.get("act_id") or "").casefold(),
            str(row.get("jurabk") or "").casefold(),
        }]
    rows.sort(key=lambda row: (
        str(row.get("observed_at") or ""),
        str(row.get("act_id") or "")), reverse=True)
    return {
        "schema_version": data.get("schema_version"),
        "state_identity": data.get("state_identity"),
        "source_policy": data.get("source_policy"),
        "total_states": data.get("total_states"),
        "total_observations": data.get("total_observations"),
        "matched": len(rows),
        "offset": offset,
        "limit": limit,
        "observations": rows[offset:offset + limit],
    }


@app.get("/official-transition-reviews")
def official_transition_reviews(
        act: str | None = Query(
            None, min_length=1, description="act id or exact jurabk"),
        procedure_id: str | None = Query(None, min_length=1),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Legal dates accepted by the final BGBl + DIP + GII state gate."""
    data = _load("official_transition_reviews")
    rows = list(data.get("reviews") or [])
    if act:
        needle = act.casefold().strip()
        rows = [row for row in rows if needle in {
            str(row.get("act_id") or "").casefold(),
            str(row.get("jurabk") or "").casefold(),
        }]
    if procedure_id:
        rows = [row for row in rows if str(
            row.get("procedure_id") or "") == procedure_id]
    rows.sort(key=lambda row: str(row.get("effective_at") or ""),
              reverse=True)
    return {
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "source_policy": data.get("source_policy"),
        "total": data.get("total"),
        "matched": len(rows),
        "offset": offset,
        "limit": limit,
        "reviews": rows[offset:offset + limit],
    }


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
def act_archive(
        act_id: str,
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice")):
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
    manifest = _retrospective_manifest(optional=True)
    knowledge = _knowledge_value(as_of)
    if manifest is None:
        payload["retrospective"] = {
            "available": False,
            "as_of": knowledge,
            "reason": "retrospective history is not built",
        }
    else:
        try:
            history = resolve_act_history(
                manifest, act_id, as_of=knowledge)
        except RetrospectiveNotFound as exc:
            if act_id in (manifest.get("acts") or {}):
                raise HTTPException(404, str(exc)) from exc
            payload["retrospective"] = {
                "available": False,
                "as_of": knowledge or manifest.get("built_at"),
                "reason": "no verified retrospective intervals for this act",
            }
        except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
            raise HTTPException(
                503, f"retrospective history integrity failure: {exc}") from exc
        else:
            payload["retrospective"] = {
                "available": True,
                "schema_version": manifest.get("schema_version"),
                "built_at": manifest.get("built_at"),
                "as_of": history.get("as_of"),
                "history_start": history.get("history_start"),
                "intervals": history.get("intervals") or [],
                "events": history.get("events") or [],
                "observations": history.get("observations") or [],
                "gaps": history.get("gaps") or [],
                "coverage": history.get("coverage") or {},
                "date_semantics": manifest.get("date_semantics") or {},
            }
    return _cached(payload)


@app.get("/acts/{act_id}/history")
def retrospective_history(
        act_id: str,
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice")):
    """Bitemporal state/event history for one act."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    try:
        history = resolve_act_history(
            manifest, act_id, as_of=_knowledge_value(as_of))
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
        raise HTTPException(
            503, f"retrospective history integrity failure: {exc}") from exc
    return _cached(_history_envelope(manifest, history))


@app.get("/acts/{act_id}/diff")
def retrospective_diff(
        act_id: str,
        from_date: date = Query(..., alias="from",
                                description="first legal date (YYYY-MM-DD)"),
        to_date: date = Query(..., alias="to",
                              description="second legal date (YYYY-MM-DD)"),
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice"),
        norm: str | None = Query(
            None, description="optional §/Art. designator")):
    """Diff two complete CAS states at one knowledge-time slice."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    try:
        payload = diff_intervals(
            manifest, act_id, from_date.isoformat(), to_date.isoformat(),
            DATA_DIR / "federal_states",
            as_of=_knowledge_value(as_of), norm=norm)
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
        raise HTTPException(
            503, f"retrospective history integrity failure: {exc}") from exc
    return _cached(payload)


@app.get("/acts/{act_id}/markdown", response_class=Response)
def act_markdown(
        act_id: str,
        at: date | None = Query(
            None, description="legal date (YYYY-MM-DD); omit for HEAD"),
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge time; requires at"),
        norm: str | None = Query(
            None, description="§/Art. designator; omit for the entire act"),
        download: bool = Query(False, description="send as a .md attachment"),
):
    """Full act or one §/Art. as raw ``text/markdown``.

    Response headers expose the resolved date and whether it is the exact HEAD
    snapshot.  Historical gaps are also embedded at the top of the Markdown.
    """
    if as_of is not None and at is None:
        raise HTTPException(422, "as_of requires an explicit legal date in at")
    requested_at = at.isoformat() if at is not None else None
    knowledge = _knowledge_value(as_of)
    try:
        act = _load_act(act_id)
        retrospective_interval = None
        retrospective_state = None
        manifest = _retrospective_manifest(optional=True)
        if requested_at is not None and manifest is not None:
            try:
                retrospective_interval = resolve_interval(
                    manifest, act_id, requested_at, as_of=knowledge)
                retrospective_state = load_interval_state(
                    retrospective_interval, DATA_DIR / "federal_states")
            except RetrospectiveNotFound:
                if knowledge is not None:
                    raise
            except (RetrospectiveIntegrityError,
                    RetrospectiveAmbiguity) as exc:
                raise HTTPException(
                    503, f"retrospective history integrity failure: {exc}") \
                    from exc
        elif knowledge is not None:
            raise HTTPException(503, "retrospective history is not built")
        observed_state = (None if retrospective_state is not None else
                          load_observed_state(DATA_DIR, act, requested_at))
        result = render_markdown_snapshot(
            act, requested_at=requested_at, norm=norm,
            fallback_head=_archive_head_fallback(),
            observed_state=observed_state,
            retrospective_state=retrospective_state,
            retrospective_interval=retrospective_interval)
    except UnknownNormError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except OfficialStateError as exc:
        # Integrity failure is a broken immutable data generation, not a bad
        # user date.  Fail closed instead of falling back to a reconstruction.
        raise HTTPException(503, str(exc)) from exc
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
    if result.get("date_basis"):
        headers["X-Lexgraph-Date-Basis"] = str(result["date_basis"])
    if result.get("state_sha256"):
        headers["X-Lexgraph-State-SHA256"] = str(result["state_sha256"])
    if result.get("source_url"):
        headers["X-Lexgraph-Source-URL"] = str(result["source_url"])
    if result.get("legal_effect_verified") is True:
        headers["X-Lexgraph-Legal-Effect-Verified"] = "true"
    if result.get("published_at"):
        headers["X-Lexgraph-Published-Date"] = str(result["published_at"])
    if result.get("effective_at"):
        headers["X-Lexgraph-Effective-Date"] = str(result["effective_at"])
    if result.get("review_id"):
        headers["X-Lexgraph-Review-ID"] = str(result["review_id"])
    if result.get("procedure_id"):
        headers["X-Lexgraph-Procedure-ID"] = str(result["procedure_id"])
    retrospective_headers = {
        "X-Lexgraph-As-Of": "as_of",
        "X-Lexgraph-Effective-From": "effective_from",
        "X-Lexgraph-Effective-To": "effective_to",
        "X-Lexgraph-Knowledge-From": "knowledge_from",
        "X-Lexgraph-Knowledge-To": "knowledge_to",
        "X-Lexgraph-Observed-Date": "observed_at",
        "X-Lexgraph-Text-Status": "text_status",
        "X-Lexgraph-Date-Status": "date_status",
    }
    for header, field in retrospective_headers.items():
        if result.get(field) is not None:
            headers[header] = str(result[field])
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


@app.get("/retrospective-history.sqlite", response_class=FileResponse)
def retrospective_sqlite():
    """Download the portable bitemporal SQLite database built with the API."""
    path = DATA_DIR / "retrospective_history.sqlite"
    if not path.is_file():
        raise HTTPException(404, "retrospective SQLite database is not built")
    return FileResponse(
        path, media_type="application/vnd.sqlite3",
        filename="lexgraph-retrospective-history.sqlite",
        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/git")
def git(lane: int | None = Query(None, ge=0, le=3),
        limit: int = Query(100, ge=1, le=1000)):
    """Laws-as-Git event log, optionally filtered by lane, newest first.

    Git is a navigation metaphor: legal effect is determined by each row's
    official source and status. Lane: 0=EU, 1=Bund, 2=Bayern, 3=Länder.
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
    all_procedure_matches = _procedure_search_index(
        _load("hierarchy")).search(q, 10_000)
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
