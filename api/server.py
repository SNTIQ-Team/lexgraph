"""Lexgraph API — the ASGI entry point.

Composition root for the Lexgraph backend. It mirrors the sibling Amtsgraph
project's `api/server.py`: a thin platform shell that adds CORS + gzip, exposes
a service index at `/` (JSON for API clients, an HTML landing for browsers)
and includes the Lexgraph routes (so they appear in /docs).

Run:
    LEXGRAPH_DATA=/path/to/web/data \
    uvicorn api.server:server --host 127.0.0.1 --port 8010 --workers 1
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from api.main import app as lexgraph

SERVICE_INDEX = {
    "service": "lexgraph-api",
    "operator": "SNTIQ n.e.V. — https://sntiq.com",
    "dataset": "https://github.com/SNTIQ-Team/lexgraph",
    "docs": "/docs",
    "endpoints": [
        "/health", "/version", "/stats", "/feed",
        "/acts", "/acts/{id}", "/decisions", "/decisions/{id}",
        "/git", "/graph", "/hierarchy", "/eu-index", "/search", "/digest",
    ],
}

server = FastAPI(
    title="Lexgraph API",
    version="1.0",
    docs_url="/docs",
    redoc_url=None,
)

server.add_middleware(GZipMiddleware, minimum_size=2048)

# CORS: allow all origins so any browser frontend can call the API
# (mirrors Amtsgraph's public, read-only data API).
server.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
    max_age=86400,
)


# Browser landing for GET / — one self-contained string, inline CSS only.
# All hrefs are RELATIVE (no leading slash): in production the app is proxied
# under /lex/ with root_path stripping, so an absolute /acts would escape the
# prefix.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lexgraph API</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #030304; color: #e7e9ee; -webkit-font-smoothing: antialiased;
         font: 15px/1.6 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 680px; margin: 0 auto; padding: 56px 24px 40px; }
  .accent { height: 3px; width: 72px; border-radius: 2px; margin-bottom: 26px;
            background: linear-gradient(90deg, #0033A0, #0052FF, #0072CE); }
  h1 { font-size: 30px; font-weight: 650; letter-spacing: -0.02em; color: #fff; }
  .tagline { margin: 10px 0 34px; color: #9aa3b2; max-width: 56ch; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 9px 12px 9px 0; vertical-align: top;
       border-bottom: 1px solid rgba(255,255,255,.07); }
  td.ep { white-space: nowrap; font-size: 13.5px;
          font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  td.what { color: #9aa3b2; }
  a { color: #4d8dff; text-decoration: none; }
  a:hover { color: #7fabff; text-decoration: underline; }
  footer { margin-top: 36px; padding-top: 18px; font-size: 13.5px; color: #6b7280;
           border-top: 1px solid rgba(255,255,255,.07); }
  footer a { color: #9aa3b2; }
  @media (max-width: 480px) { main { padding: 36px 18px; } h1 { font-size: 24px; } }
</style>
</head>
<body>
<main>
  <div class="accent"></div>
  <div style="display:flex;align-items:center;gap:14px;">
    <img src="favicon.svg" width="40" height="40" alt="SNTIQ mark">
    <h1>Lexgraph API</h1>
  </div>
  <p class="tagline">German &amp; EU legislation as event-sourced git —
     every law a repository, every amendment a commit.</p>
  <table>
    <tr><td class="ep"><a href="health">/health</a></td><td class="what">liveness + data-plane check</td></tr>
    <tr><td class="ep"><a href="version">/version</a></td><td class="what">dataset + build timestamp</td></tr>
    <tr><td class="ep"><a href="stats">/stats</a></td><td class="what">dashboard counts</td></tr>
    <tr><td class="ep"><a href="feed">/feed</a></td><td class="what">realtime event stream, newest first</td></tr>
    <tr><td class="ep"><a href="acts">/acts</a></td><td class="what">the act index (federal + Bavaria)</td></tr>
    <tr><td class="ep">/acts/{id}</td><td class="what">one full act — head, patches, versions, norms</td></tr>
    <tr><td class="ep"><a href="decisions">/decisions</a></td><td class="what">court decisions (Rechtsprechung)</td></tr>
    <tr><td class="ep">/decisions/{id}</td><td class="what">one complete exported decision row</td></tr>
    <tr><td class="ep"><a href="git">/git</a></td><td class="what">the commit-graph of lawmaking</td></tr>
    <tr><td class="ep"><a href="graph">/graph</a></td><td class="what">the QFS arena export</td></tr>
    <tr><td class="ep"><a href="hierarchy">/hierarchy</a></td><td class="what">jurisdiction tree (EU / Bund / Bayern / Länder)</td></tr>
    <tr><td class="ep"><a href="eu-index">/eu-index</a></td><td class="what">all in-force EU directives + basic regulations</td></tr>
    <tr><td class="ep"><a href="search?q=asyl">/search?q=</a></td><td class="what">search acts by jurabk / title</td></tr>
    <tr><td class="ep"><a href="digest">/digest</a></td><td class="what">LLM digest of legislative activity (experimental)</td></tr>
  </table>
  <footer>
    <a href="docs">Interactive docs</a> &nbsp;·&nbsp;
    <a href="https://github.com/SNTIQ-Team/lexgraph">Dataset</a> &nbsp;·&nbsp;
    operated by <a href="https://sntiq.com">SNTIQ&nbsp;n.e.V.</a>
  </footer>
</main>
</body>
</html>
"""


@server.get("/")
def index(request: Request):
    """Service index — JSON for API clients, an HTML landing for browsers."""
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(INDEX_HTML)
    return SERVICE_INDEX


# The real SNTIQ mark (same file the website uses), served for the landing
# page header and the browser tab icon.
_MARK = Path(__file__).resolve().parent / "sntiq-mark.svg"


@server.get("/favicon.svg", include_in_schema=False)
@server.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(_MARK, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})


# ---- included service -----------------------------------------------------
# Lexgraph currently ships a single service. Its routes are INCLUDED (not
# mounted) so they land in this app's OpenAPI schema — a mounted sub-app's
# routes are invisible to /docs, which used to show only "/" and "/health".
# The endpoint paths stay flat (/acts, /git, …) as documented; /health comes
# from api.main.

server.include_router(lexgraph.router)
