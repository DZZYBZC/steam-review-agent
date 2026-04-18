"""
Live run routes — POST a review to start a new run, stream the SSE events.

Task 5: start + pre-gate stream only. Resume + human gate decision handled
by task 6 in POST /runs/{run_id}/resume.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import PROPOSED_ACTIONS
from backend.services.live_runner import (
    LiveRunError,
    inject_human_decision,
    lookup_run,
    mint_ids,
    register_run,
    run_live,
    run_live_resume,
)
from backend.services.sse import format_sse

logger = logging.getLogger(__name__)
router = APIRouter()


class StartRunRequest(BaseModel):
    app_id: str
    review_id: str


class StartRunResponse(BaseModel):
    run_id: str
    thread_id: str


@router.post("/runs", response_model=StartRunResponse)
def start_run(req: StartRunRequest) -> StartRunResponse:
    """Mint a run_id and thread_id for a live run. Client then opens an
    EventSource on /runs/{run_id}/stream to drive the actual execution."""
    run_id, thread_id = mint_ids()
    register_run(run_id, thread_id, req.app_id, req.review_id)
    logger.info("Registered live run %s (thread=%s) for %s:%s", run_id, thread_id, req.app_id, req.review_id)
    return StartRunResponse(run_id=run_id, thread_id=thread_id)


@router.get("/runs/{run_id}/stream")
def stream_run(
    run_id: str,
    from_: str | None = Query(None, alias="from"),
) -> StreamingResponse:
    """Stream canonical SSE events for a live run.

    Without `from`, runs the graph from initial state (first pre-gate stream).
    With `from=resume`, resumes a paused graph after POST /runs/{id}/resume
    has injected the human_decision.
    """
    info = lookup_run(run_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found (call POST /runs first)")

    if from_ == "resume":
        def event_generator() -> Iterator[str]:
            for event in run_live_resume(run_id=run_id, thread_id=info["thread_id"]):
                yield format_sse(event)
    else:
        def event_generator() -> Iterator[str]:  # type: ignore[no-redef]
            for event in run_live(
                app_id=info["app_id"],
                review_id=info["review_id"],
                run_id=run_id,
                thread_id=info["thread_id"],
            ):
                yield format_sse(event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class ResumeRequest(BaseModel):
    human_decision: str = Field(..., pattern="^(approved|rejected)$")
    human_feedback: str = ""
    human_action_override: str = ""


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: str, req: ResumeRequest) -> Response:
    """Inject a human decision into the paused graph. Does not stream —
    the client then opens a new EventSource on /runs/{id}/stream?from=resume
    to receive the post-gate events.
    """
    info = lookup_run(run_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")

    override = (req.human_action_override or "").strip()
    if override and override not in PROPOSED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"human_action_override must be empty or one of: {list(PROPOSED_ACTIONS)}",
        )

    try:
        inject_human_decision(
            thread_id=info["thread_id"],
            human_decision=req.human_decision,
            human_feedback=req.human_feedback or "",
            human_action_override=override,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("resume failed")
        raise HTTPException(status_code=500, detail=str(e))

    return Response(status_code=202)
