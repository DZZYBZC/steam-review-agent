"""
FastAPI app for the Steam Review Triage demo UI.

Boots with a lifespan hook that warm-loads the embedding and reranker models
so the first live run doesn't pay model-load cost. Exposes two probes:

- GET /healthz — liveness, returns 200 if the process is up
- GET /ready   — interview pre-flight, returns 200 only when all checks pass
                 (models warmed, featured run_ids valid, latest eval snapshot
                 found, both SQLite DBs reachable)

Run locally:
    uvicorn backend.app:app --reload
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import CHECKPOINT_DB_PATH, DB_PATH
from backend.routes import chunks as chunks_route
from backend.routes import cm as cm_route
from backend.routes import evals as evals_route
from backend.routes import notes as notes_route
from backend.routes import replays as replays_route
from backend.routes import reviews as reviews_route
from backend.routes import runs as runs_route
from backend.services.featured import FEATURED_CASES

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = PROJECT_ROOT / "evals" / "snapshots"
FRONTEND_DIR = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm-load models at startup; record timings on app.state."""
    from pipeline.retrieve import _get_model, _get_reranker

    logger.info("Warm-loading embedding model")
    t0 = time.monotonic()
    _get_model()
    app.state.embedding_ms = int((time.monotonic() - t0) * 1000)

    logger.info("Warm-loading reranker model")
    t0 = time.monotonic()
    _get_reranker()
    app.state.reranker_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "Warm-load complete: embedding=%dms, reranker=%dms",
        app.state.embedding_ms,
        app.state.reranker_ms,
    )
    yield


app = FastAPI(title="Steam Review Triage — Demo", lifespan=lifespan)
app.include_router(replays_route.router)
app.include_router(chunks_route.router)
app.include_router(reviews_route.router)
app.include_router(runs_route.router)
app.include_router(evals_route.router)
app.include_router(notes_route.router)
app.include_router(cm_route.router)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


def _check_models_warmed() -> dict[str, Any]:
    embedding_ms = getattr(app.state, "embedding_ms", None)
    reranker_ms = getattr(app.state, "reranker_ms", None)
    ok = embedding_ms is not None and reranker_ms is not None
    return {"ok": ok, "embedding_ms": embedding_ms, "reranker_ms": reranker_ms}


def _check_featured_cases_valid() -> dict[str, Any]:
    if not FEATURED_CASES:
        return {
            "ok": False,
            "n_valid": 0,
            "missing_run_ids": [],
            "note": "FEATURED_CASES is empty — run curation script",
        }
    run_ids = [case["run_id"] for case in FEATURED_CASES]
    placeholders = ",".join("?" * len(run_ids))
    try:
        with sqlite3.connect(DB_PATH) as conn:
            found = {
                row[0]
                for row in conn.execute(
                    f"SELECT run_id FROM audit_log WHERE run_id IN ({placeholders})",
                    run_ids,
                )
            }
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    missing = [rid for rid in run_ids if rid not in found]
    return {
        "ok": not missing,
        "n_valid": len(run_ids) - len(missing),
        "missing_run_ids": missing,
    }


def _check_latest_snapshot_found() -> dict[str, Any]:
    if not SNAPSHOTS_DIR.exists():
        return {"ok": False, "error": f"{SNAPSHOTS_DIR} does not exist"}
    snapshots = sorted(SNAPSHOTS_DIR.glob("snapshot_*.json"))
    if not snapshots:
        return {"ok": False, "error": "no snapshot files found"}
    latest = snapshots[-1]
    age_days = int((time.time() - latest.stat().st_mtime) / 86400)
    return {
        "ok": True,
        "path": str(latest.relative_to(PROJECT_ROOT)),
        "age_days": age_days,
    }


def _check_reviews_db() -> dict[str, Any]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            n_audit = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            n_candidates = conn.execute(
                """
                SELECT COUNT(*) FROM reviews r
                LEFT JOIN audit_log al ON r.review_id = al.review_id
                WHERE al.review_id IS NULL
                  AND r.is_near_duplicate = 0
                """
            ).fetchone()[0]
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "n_audit_rows": n_audit, "n_candidates": n_candidates}


def _check_checkpoints_db() -> dict[str, Any]:
    if not os.path.exists(CHECKPOINT_DB_PATH):
        return {"ok": False, "error": f"{CHECKPOINT_DB_PATH} does not exist"}
    try:
        with sqlite3.connect(CHECKPOINT_DB_PATH) as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@app.get("/ready")
def ready() -> JSONResponse:
    checks = {
        "models_warmed": _check_models_warmed(),
        "featured_cases_valid": _check_featured_cases_valid(),
        "latest_snapshot_found": _check_latest_snapshot_found(),
        "reviews_db_reachable": _check_reviews_db(),
        "checkpoints_db_reachable": _check_checkpoints_db(),
    }
    all_ok = all(check["ok"] for check in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"ok": all_ok, "checks": checks},
    )
