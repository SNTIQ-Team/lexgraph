"""Lexgraph API — the ASGI entry point.

Composition root for the Lexgraph backend. It mirrors the sibling Amtsgraph
project's `api/server.py`: a thin platform shell that adds CORS + gzip, exposes
a service index at `/` and a top-level `/health`, then mounts the Lexgraph app.

Run:
    LEXGRAPH_DATA=/path/to/web/data \
    uvicorn api.server:server --host 127.0.0.1 --port 8010 --workers 1
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from api.main import DATA_DIR, _load, app as lexgraph

SERVICE_INDEX = {
    "service": "lexgraph-api",
    "operator": "SNTIQ n.e.V. — https://sntiq.com",
    "dataset": "https://github.com/SNTIQ-Team/lexgraph",
    "docs": "/docs",
    "endpoints": [
        "/health", "/version", "/stats", "/feed",
        "/acts", "/acts/{id}", "/git", "/graph",
        "/hierarchy", "/search",
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


@server.get("/")
def index():
    return SERVICE_INDEX


@server.get("/health")
def health():
    try:
        summary = _load("summary")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"data plane unavailable: {exc}")
    return {"status": "ok", "service": "lexgraph",
            "built_at": summary.get("built_at"), "data_dir": str(DATA_DIR)}


# ---- mounted service ------------------------------------------------------
# Lexgraph currently ships a single service; mounting at root keeps the
# endpoint paths flat (/acts, /git, …) as documented.

server.mount("/", lexgraph)
