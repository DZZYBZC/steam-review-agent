"""
CM (Community Manager) batch route.

POST /cm/run mints a meta_run_id and streams the canonical SSE event sequence.
POST /cm/{meta_run_id}/confirm resumes a CM run paused at the deterministic
human-confirmation gate (the planner doesn't have its own LLM-level gate tool;
gating is decided in cm_runner based on uncertain/concerns/filter signals).
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterator, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.services.cm_runner import resolve_pending_confirm, run_cm
from backend.services.sse import format_sse

logger = logging.getLogger(__name__)
router = APIRouter()


class CMRunRequest(BaseModel):
    goal: str = Field(..., min_length=1)


class CMConfirmRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    # When the gate surfaces multiple in-DB matches for a typed game name, the
    # user's pick is sent here. Server validates it against the planner's
    # alternatives list before applying. Empty / omitted = no override.
    chosen_app_id: str = ""


@router.post("/cm/run")
def start_cm_run(req: CMRunRequest) -> StreamingResponse:
    """Start a CM batch run and stream its SSE events."""
    meta_run_id = str(uuid.uuid4())
    logger.info("Starting CM run %s for goal=%r", meta_run_id, req.goal)

    def event_generator() -> Iterator[str]:
        for event in run_cm(goal=req.goal, meta_run_id=meta_run_id):
            yield format_sse(event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cm/{meta_run_id}/confirm", status_code=202)
def cm_confirm(meta_run_id: str, req: CMConfirmRequest) -> JSONResponse:
    """Resume a CM run paused at the deterministic human-confirmation gate.

    Mirrors backend/routes/runs.py's resume pattern but at the meta-run level —
    the agent-graph's per-sub-run gate is auto-approved silently inside
    _drive_sub_run_streaming; THIS gate is the only user-visible CM-level confirmation.
    """
    ok = resolve_pending_confirm(meta_run_id, req.decision, req.chosen_app_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"meta_run_id={meta_run_id} is not awaiting confirmation",
        )
    logger.info(
        "CM run %s resumed with decision=%r chosen_app_id=%r",
        meta_run_id, req.decision, req.chosen_app_id,
    )
    return JSONResponse(
        {"status": "accepted", "decision": req.decision, "chosen_app_id": req.chosen_app_id},
        status_code=202,
    )
