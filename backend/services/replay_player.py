"""
Replay a completed run from persisted state.

Given a `run_id`, pulls the final-state row from `audit_log`, the per-iteration
history from `audit_log_iterations`, and the triggering review text from
`reviews`. Synthesizes the same canonical SSE event sequence a live run
would emit — the frontend reducer is blind to the source.

Fields that aren't persisted (full evidence_package, wall_clock_ms,
retrieval_call_count) are filled with conservative defaults or inferred
heuristically; this is explicit, not silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any, AsyncIterator

from config import DB_PATH, HYDE_ENABLED, RERANKER_TOP_N
from backend.services.featured import FEATURED_CASES
from backend.services.sse import SPEED_DELAYS_MS, envelope

logger = logging.getLogger(__name__)


class ReplayNotFound(Exception):
    """Raised when the requested run_id does not exist in audit_log."""


def _fetch_run(run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch audit row, iteration rows, and the review row for a run_id."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ?", (run_id,)
        ).fetchone()
        if audit_row is None:
            raise ReplayNotFound(f"run_id {run_id!r} not found in audit_log")
        iter_rows = conn.execute(
            """
            SELECT * FROM audit_log_iterations
            WHERE run_id = ?
            ORDER BY iteration ASC
            """,
            (run_id,),
        ).fetchall()
        review_row = conn.execute(
            "SELECT * FROM reviews WHERE review_id = ?", (audit_row["review_id"],)
        ).fetchone()
    return (
        dict(audit_row),
        [dict(r) for r in iter_rows],
        dict(review_row) if review_row else None,
    )


def _parse_json_field(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _sum_tokens(token_usage_raw: str | None) -> int:
    """Sum all input+output tokens across nodes from the token_usage JSON dict."""
    parsed = _parse_json_field(token_usage_raw, {})
    if not isinstance(parsed, dict):
        return 0
    total = 0
    for node_counts in parsed.values():
        if isinstance(node_counts, dict):
            total += int(node_counts.get("input", 0)) + int(node_counts.get("output", 0))
    return total


def _infer_action_freeze(iter_rows: list[dict[str, Any]], audit: dict[str, Any]) -> bool:
    """Action-freeze fires when the final iteration was rejected with reason_type='action'
    but the run still reached human_approved. No direct flag in audit_log; inferred."""
    if not iter_rows:
        return False
    last = iter_rows[-1]
    return (
        not last.get("approved")
        and last.get("reason_type") == "action"
        and audit.get("stop_reason") == "human_approved"
    )


def build_events(run_id: str) -> list[dict[str, Any]]:
    """Build the full canonical SSE event sequence for a completed run."""
    audit, iter_rows, review = _fetch_run(run_id)
    events: list[dict[str, Any]] = []

    def emit(type_: str, iteration: int, payload: dict[str, Any]) -> None:
        events.append(envelope(type=type_, run_id=run_id, iteration=iteration, payload=payload))

    # Per-iteration reconstruction.
    for i, iter_row in enumerate(iter_rows):
        iteration = int(iter_row["iteration"])
        emit("node_start", iteration, {"node": "coordinator"})

        # Investigator runs on iter 0, or after a prior "evidence" rejection.
        needs_investigator = i == 0 or iter_rows[i - 1].get("reason_type") == "evidence"
        if needs_investigator:
            emit("node_start", iteration, {"node": "investigator"})
            # Synthetic retrieval sub-steps — describe the pipeline stages that
            # the investigator's retrieve_patches tool always runs. Per-stage
            # counts aren't persisted for replay; the labels describe the
            # architecture. Live runner (task 5) will emit real per-call stats.
            sources_cited = _parse_json_field(audit.get("source_ids_cited"), [])
            ev_conf = audit.get("evidence_confidence")
            ev_summary = audit.get("evidence_summary") or ""
            retrieval_decision = audit.get("retrieval_decision") or "retrieve"

            emit("retrieval_step", iteration, {
                "stage": "hybrid_candidates",
                "label": "Hybrid candidates",
                "description": "BM25 + vector (bge-base-en-v1.5) → top-10 each",
                "details": {
                    "inputs": [
                        "keyword query (derived from review + category)",
                        "patches_{app_id} ChromaDB collection + in-memory BM25 index",
                    ],
                    "operations": [
                        "BM25Okapi rank over tokenized chunk documents → top 10",
                        "Vector similarity (cosine) over bge-base-en-v1.5 embeddings → top 10",
                    ],
                    "output": "~20 candidate chunks (with overlap allowed)",
                    "note": (
                        "Per-stage candidate lists aren't persisted to audit_log; "
                        "the live runner (task 5) will surface the live candidate set."
                    ),
                },
            })
            if HYDE_ENABLED:
                emit("retrieval_step", iteration, {
                    "stage": "hyde",
                    "label": "HyDE expansion",
                    "description": "Hypothetical patch note → embed → top-5",
                    "details": {
                        "inputs": ["keyword query"],
                        "operations": [
                            "Haiku generates a plausible hypothetical patch note for the query",
                            "Embed the hypothetical with bge-base-en-v1.5",
                            "Vector search top-5 + diversity cap of 2 per parent_id",
                        ],
                        "output": "up to 5 additional candidate chunks",
                        "note": (
                            "HyDE bridges lexical gaps when the review uses casual language "
                            "('stuttery mess') but patches use technical phrasing "
                            "('frame-pacing optimization'). Flagged by HYDE_ENABLED."
                        ),
                    },
                })
            emit("retrieval_step", iteration, {
                "stage": "rrf_fusion",
                "label": "RRF fusion",
                "description": "Reciprocal rank fusion across retrievers",
                "details": {
                    "inputs": ["BM25 top-10", "vector top-10"] + (["HyDE top-5"] if HYDE_ENABLED else []),
                    "operations": [
                        "For each chunk, sum 1 / (RRF_K + rank_in_retriever) across retrievers",
                        "RRF_K = 60 (mainstream default; larger K reduces influence of any single retriever's top rank)",
                    ],
                    "output": "merged, rank-fused candidate list",
                    "note": (
                        "RRF is rank-based, not score-based — avoids anchoring on absolute "
                        "scores across retrievers that use incomparable scales (BM25 vs cosine)."
                    ),
                },
            })
            emit("retrieval_step", iteration, {
                "stage": "gemma_rerank",
                "label": "Gemma rerank",
                "description": f"bge-reranker-v2-gemma → top {RERANKER_TOP_N}",
                "details": {
                    "inputs": ["RRF-fused candidate list", "original keyword query"],
                    "operations": [
                        "BAAI/bge-reranker-v2-gemma rescores each (query, chunk) pair via LLM reranker",
                        "Sort by reranker score",
                        f"Keep top {RERANKER_TOP_N}",
                        "Attach parent-context window (±3 sibling bullets, clipped to 2000 chars) to each kept chunk",
                    ],
                    "output": f"{RERANKER_TOP_N} chunks with parent context windows",
                    "note": (
                        "Gemma reranker scores are NOT passed to the investigator LLM "
                        "— they're uncalibrated floats and anchoring on them would "
                        "amplify noise. The reranker orders; the investigator reasons about content."
                    ),
                },
            })
            emit("retrieval_step", iteration, {
                "stage": "summarize",
                "label": "Evidence package",
                "description": (
                    f"{len(sources_cited)} chunks cited · "
                    f"decision={retrieval_decision} · "
                    f"confidence={ev_conf if ev_conf is not None else 'n/a'}"
                ),
                "details": {
                    "inputs": [f"top {RERANKER_TOP_N} reranked chunks with parent context"],
                    "operations": [
                        "Investigator LLM (Sonnet 4.6) reasons over chunks via tool-use loop "
                        "(up to INVESTIGATOR_MAX_TOOL_CALLS=4 query reformulations + 1 secondary probe)",
                        "Produces EvidencePackage: {summary, confidence, retrieval_decision, source_ids, relevant_ids, known_unknowns}",
                        "Chain of custody: source_ids (retrieved) → relevant_ids (investigator's filter) → source_ids_cited (responder's citations). Critic verifies source_ids_cited ⊆ relevant_ids deterministically.",
                    ],
                    "output": f"{len(sources_cited)} chunk ids the responder cited",
                    "cited_chunk_ids": sources_cited,
                    "evidence_confidence": ev_conf,
                    "retrieval_decision": retrieval_decision,
                },
            })
            # Per-iteration evidence isn't persisted; best approximation is the
            # final-state evidence_summary from audit_log.
            emit(
                "state_update",
                iteration,
                {
                    "node": "investigator",
                    "updates": {
                        "evidence_package": {
                            "summary": ev_summary,
                            "retrieval_decision": retrieval_decision,
                            "confidence": ev_conf,
                            "source_ids": sources_cited,
                        }
                    },
                },
            )

        emit("node_start", iteration, {"node": "responder"})
        emit(
            "state_update",
            iteration,
            {
                "node": "responder",
                "updates": {
                    "drafted_response": iter_row.get("drafted_response") or "",
                    "proposed_action": iter_row.get("proposed_action") or "",
                    "source_ids_cited": _parse_json_field(iter_row.get("source_ids_cited"), []),
                },
            },
        )

        emit("node_start", iteration, {"node": "critic"})
        emit(
            "state_update",
            iteration,
            {
                "node": "critic",
                "updates": {
                    "critique": iter_row.get("critique") or "",
                    "approved": bool(iter_row.get("approved")),
                    "revision_reason": iter_row.get("revision_reason") or "",
                    "reason_type": iter_row.get("reason_type") or "",
                    "retrieval_hint": iter_row.get("retrieval_hint") or "",
                },
            },
        )

    # Terminal events.
    stop_reason = audit.get("stop_reason") or ""
    final_iteration = int(audit.get("iteration_count") or 0)

    if stop_reason in ("llm_error", "parse_error"):
        emit(
            "error",
            final_iteration,
            {
                "node": "unknown",
                "message": f"Run terminated with stop_reason={stop_reason}",
                "recoverable": False,
            },
        )
        return events

    action_freeze_applied = _infer_action_freeze(iter_rows, audit)
    frozen_action = audit.get("proposed_action") if action_freeze_applied else None

    # Presentation override — lets a featured case show a different terminal
    # decision than the DB recorded. DB data is unchanged; only replay differs.
    override = _get_demo_override(run_id)
    decision = (override or {}).get("human_decision") or audit.get("human_decision") or ""
    human_feedback = (override or {}).get("human_feedback") or audit.get("human_feedback") or ""

    emit(
        "human_gate_open",
        final_iteration,
        {
            "current_draft": audit.get("drafted_response") or "",
            "current_action": audit.get("proposed_action") or "",
            "current_critique": audit.get("critique") or "",
            "frozen_action": frozen_action,
            "action_freeze_applied": action_freeze_applied,
            "iteration_count": final_iteration,
            "evidence_summary": audit.get("evidence_summary") or "",
            "source_ids_cited": _parse_json_field(audit.get("source_ids_cited"), []),
        },
    )

    final_stop_reason = "human_rejected" if decision == "rejected" else stop_reason
    token_usage_parsed = _parse_json_field(audit.get("token_usage"), {})
    if not isinstance(token_usage_parsed, dict):
        token_usage_parsed = {}
    emit(
        "run_complete",
        final_iteration,
        {
            "stop_reason": final_stop_reason,
            "human_decision": decision,
            "human_feedback": human_feedback,
            "human_action_override": "",  # not persisted separately from proposed_action
            "final_action": audit.get("proposed_action") or "",
            "iteration_count": final_iteration,
            "total_tokens": _sum_tokens(audit.get("token_usage")),
            "token_usage_by_node": token_usage_parsed,
            "retrieval_call_count": 0,  # not persisted — unrecoverable from replay
            "wall_clock_ms": 0,  # not persisted — unrecoverable from replay
            "is_demo_override": bool(override),
        },
    )
    return events


def _get_demo_override(run_id: str) -> dict[str, Any] | None:
    for case in FEATURED_CASES:
        if case["run_id"] == run_id:
            return case.get("demo_override")
    return None


async def replay_stream(run_id: str, speed: str = "normal") -> AsyncIterator[dict[str, Any]]:
    """Yield replay events with inter-event delay scaled by `speed`."""
    delay_s = SPEED_DELAYS_MS.get(speed, SPEED_DELAYS_MS["normal"]) / 1000.0
    events = build_events(run_id)
    for i, event in enumerate(events):
        if i > 0 and delay_s > 0:
            await asyncio.sleep(delay_s)
        yield event
