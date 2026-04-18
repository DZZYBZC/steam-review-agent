"""
Replay routes: list curated replays and stream a replay's SSE event sequence.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from config import DB_PATH
from backend.services.featured import FEATURED_CASES
from backend.services.games import game_name
from backend.services.replay_player import ReplayNotFound, build_events, replay_stream
from backend.services.sse import format_sse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/replays")
def list_replays() -> dict[str, Any]:
    """Return the curated featured cases with their labels, stories, and panel hints.

    Invalid run_ids (missing from audit_log) are hidden rather than silently
    replaced — matches the readiness-check contract.
    """
    if not FEATURED_CASES:
        return {"cases": [], "note": "FEATURED_CASES is empty — populate backend/services/featured.py"}

    run_ids = [case["run_id"] for case in FEATURED_CASES]
    placeholders = ",".join("?" * len(run_ids))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT run_id, app_id, review_id, category, iteration_count, stop_reason
            FROM audit_log
            WHERE run_id IN ({placeholders})
            """,
            run_ids,
        ).fetchall()
    by_run_id = {row["run_id"]: dict(row) for row in rows}

    cases = []
    missing = []
    for case in FEATURED_CASES:
        meta = by_run_id.get(case["run_id"])
        if meta is None:
            missing.append(case["run_id"])
            continue
        meta = {**meta, "game_name": game_name(meta.get("app_id"))}
        cases.append({**case, "meta": meta})

    if missing:
        logger.warning("Hiding %d featured case(s) with missing run_ids: %s", len(missing), missing)

    return {"cases": cases, "hidden_missing_count": len(missing)}


@router.get("/replays/{run_id}")
def get_replay_meta(run_id: str) -> dict[str, Any]:
    """Return full metadata + review text for a featured replay, for the frontend
    to render the input panel before opening the SSE stream."""
    case = next((c for c in FEATURED_CASES if c["run_id"] == run_id), None)
    if case is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} is not a featured case")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT al.run_id, al.app_id, al.review_id, al.category, al.review_tone,
                   al.iteration_count, al.stop_reason, al.review_text
            FROM audit_log al
            WHERE al.run_id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found in audit_log")
    meta = dict(row)
    meta["game_name"] = game_name(meta.get("app_id"))
    return {**case, "meta": meta}


@router.get("/replays/{run_id}/stream")
async def stream_replay(
    run_id: str,
    speed: str = Query("normal", pattern="^(instant|normal|slow)$"),
) -> StreamingResponse:
    """Stream the canonical SSE event sequence for a completed run."""
    # Validate up front so clients get a clean 404, not an SSE containing an error.
    try:
        build_events(run_id)
    except ReplayNotFound:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")

    async def event_generator() -> AsyncIterator[str]:
        async for event in replay_stream(run_id, speed=speed):
            yield format_sse(event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering if ever fronted
        },
    )
