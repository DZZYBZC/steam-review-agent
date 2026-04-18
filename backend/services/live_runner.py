"""
Live run execution — wraps the agent graph and translates LangGraph's stream
output into the canonical SSE schema used by the frontend.

Emits the same event shapes as replay_player so the UI reducer is blind to
the source. Terminates the stream at the human_approval interrupt with a
`human_gate_open` event; resume is handled by the /runs/{run_id}/resume route
(task 6).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any, Iterator

from config import DB_PATH, HYDE_ENABLED, RERANKER_TOP_N
from agent.graph import build_graph
from agent.state import AgentState
from backend.services.sse import envelope

logger = logging.getLogger(__name__)


# Compiled graph cached at module level — built lazily so imports don't fire
# the checkpointer connection + node imports until the first live run.
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        logger.info("Building agent graph for live runs")
        _graph = build_graph()
    return _graph


class LiveRunError(Exception):
    """Wraps errors that occur while streaming a live run."""


def _fetch_review_and_classification(app_id: str, review_id: str) -> dict[str, Any]:
    """Return review_text + classifier fields for a review, or raise if not found."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT r.review_id, r.review_text, r.voted_up, r.is_near_duplicate,
                   c.primary_category, c.secondary_aspects
            FROM reviews r
            LEFT JOIN classifications c ON r.review_id = c.review_id
            WHERE r.app_id = ? AND r.review_id = ?
            """,
            (app_id, review_id),
        ).fetchone()
    if row is None:
        raise LiveRunError(f"review_id {review_id!r} not found for app_id {app_id!r}")
    row = dict(row)
    if not row.get("primary_category"):
        raise LiveRunError(
            f"review_id {review_id!r} has no classification; run the classifier pipeline first"
        )
    # secondary_aspects is stored as a JSON string
    import json
    try:
        row["secondary_aspects"] = json.loads(row.get("secondary_aspects") or "[]")
    except (json.JSONDecodeError, TypeError):
        row["secondary_aspects"] = []
    return row


def build_initial_state(app_id: str, review_id: str, run_id: str) -> AgentState:
    """Build an AgentState dict ready to hand to the graph."""
    r = _fetch_review_and_classification(app_id, review_id)
    state: AgentState = {
        "app_id": app_id,
        "review_id": review_id,
        "review_text": r["review_text"],
        "cluster_summary": {
            "category": r["primary_category"],
            "total_reviews": 1,
            "priority_score": 0.0,
            "secondary_aspects": r.get("secondary_aspects", []),
        },
        "review_tone": "",
        "iteration_count": 0,
        "approved": False,
        "revision_reason": "",
        "reason_type": "",
        "retrieval_hint": "",
        "stop_reason": "",
        "evidence_package": {},
        "drafted_response": "",
        "proposed_action": "",
        "source_ids_cited": [],
        "critique": "",
        "critic_preferred_action": "",
        "node_log": [],
        "token_usage": {},
        "human_decision": "",
        "human_feedback": "",
        "human_action_override": "",
        "run_id": run_id,
        "frozen_action": "",
        "action_freeze_applied": False,
        "action_override_count": 0,
        "first_override_at_iteration": -1,
        "action_filter_skip_count": 0,
        "is_eval": False,
        "is_demo": True,  # tag all interview-demo live runs so they skip promotion
    }
    return state


def _retrieval_steps(evidence_package: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the 5 architectural retrieval-stage events for an investigator turn.
    Shape mirrors replay_player so the frontend reducer is blind to the source.
    """
    source_ids = evidence_package.get("source_ids") or []
    ev_conf = evidence_package.get("confidence")
    retrieval_decision = evidence_package.get("retrieval_decision") or "retrieve"
    steps = [
        {
            "stage": "hybrid_candidates",
            "label": "Hybrid candidates",
            "description": "BM25 + vector (bge-base-en-v1.5) → top-10 each",
        },
    ]
    if HYDE_ENABLED:
        steps.append({
            "stage": "hyde",
            "label": "HyDE expansion",
            "description": "Hypothetical patch note → embed → top-5",
        })
    steps.extend([
        {
            "stage": "rrf_fusion",
            "label": "RRF fusion",
            "description": "Reciprocal rank fusion across retrievers",
        },
        {
            "stage": "gemma_rerank",
            "label": "Gemma rerank",
            "description": f"bge-reranker-v2-gemma → top {RERANKER_TOP_N}",
        },
        {
            "stage": "summarize",
            "label": "Evidence package",
            "description": (
                f"{len(source_ids)} source ids · "
                f"decision={retrieval_decision} · "
                f"confidence={ev_conf if ev_conf is not None else 'n/a'}"
            ),
        },
    ])
    return steps


def _thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _is_at_gate(graph_state) -> bool:
    """True if the graph is paused at the human_approval interrupt."""
    nxt = getattr(graph_state, "next", ()) or ()
    return "human_approval" in tuple(nxt)


def run_live(
    app_id: str,
    review_id: str,
    run_id: str,
    thread_id: str,
) -> Iterator[dict[str, Any]]:
    """Sync generator yielding canonical SSE event envelopes for a live run.

    Terminates at the human_approval interrupt (emitting `human_gate_open`) or
    on terminal stop_reasons / exceptions. Sync because the SqliteSaver
    checkpointer doesn't support async; FastAPI streams sync generators in a
    threadpool, so this does not block the event loop.
    """
    graph = _get_graph()
    thread_config = _thread_config(thread_id)

    try:
        initial = build_initial_state(app_id, review_id, run_id)
    except LiveRunError as e:
        yield envelope("error", run_id=run_id, iteration=0, payload={
            "node": "unknown",
            "message": str(e),
            "recoverable": False,
        })
        return

    # Iteration tracking: mirrors replay semantics. A new iteration starts each
    # time the critic rejects. Events before the first critic are iter 0; after
    # the first rejection, iter 1; and so on. Terminal events (final coordinator
    # routing + human_gate) stay in the last iteration (no post-approval bump).
    current_iteration = 0

    def _wrap(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return envelope(type_, run_id=run_id, iteration=current_iteration, payload=payload)

    try:
        # LangGraph's stream yields {node_name: updates_dict} after each node runs
        for update_event in graph.stream(
            initial,
            config=thread_config,
            stream_mode="updates",
        ):
            if not isinstance(update_event, dict):
                continue
            for node_name, updates in update_event.items():
                # Skip LangGraph's synthetic __interrupt__ events — they're not
                # real graph nodes; the interrupt is handled after the stream
                # drains (see _is_at_gate check below).
                if node_name.startswith("__"):
                    continue

                if not isinstance(updates, dict):
                    updates = {}

                yield _wrap("node_start", {"node": node_name})

                # Coordinator emits no meaningful state the UI renders; skip its
                # state_update to match replay semantics (replay emits only
                # coordinator node_starts).
                if node_name == "coordinator":
                    continue

                # Investigator — emit synthetic retrieval stages before its state_update.
                if node_name == "investigator":
                    ep = updates.get("evidence_package") or {}
                    for step in _retrieval_steps(ep):
                        yield _wrap("retrieval_step", step)

                yield _wrap("state_update", {"node": node_name, "updates": updates})

                # After a critic rejection, bump iteration ONLY when a new draft
                # will actually be generated. Action-only rejections get intercepted
                # by the action-freeze path (coordinator routes to the gate without
                # re-drafting) — bumping would strand the gate event in an empty
                # iteration slot and blank out Why / Draft / Evidence panels.
                if node_name == "critic" and not updates.get("approved", False):
                    reason_type = updates.get("reason_type", "")
                    if reason_type in ("drafting", "evidence"):
                        current_iteration += 1
    except Exception as e:  # noqa: BLE001 — surface any LangGraph/node error as SSE error
        logger.exception("live run failed")
        yield _wrap("error", {
            "node": "unknown",
            "message": f"{type(e).__name__}: {e}",
            "recoverable": False,
        })
        return

    # Stream drained — figure out if we're at the gate or terminal.
    try:
        final_state = graph.get_state(thread_config)
    except Exception as e:  # noqa: BLE001
        logger.exception("failed to read final graph state")
        yield _wrap("error", {
            "node": "unknown",
            "message": f"failed to read final state: {e}",
            "recoverable": False,
        })
        return

    values = final_state.values or {}
    stop_reason = values.get("stop_reason") or ""

    if stop_reason in ("llm_error", "parse_error"):
        yield _wrap("error", {
            "node": "unknown",
            "message": f"Run terminated with stop_reason={stop_reason}",
            "recoverable": False,
        })
        return

    if _is_at_gate(final_state):
        # Persist iteration so run_live_resume can continue from here.
        _save_iteration(run_id, current_iteration)
        yield _wrap("human_gate_open", {
            "current_draft": values.get("drafted_response") or "",
            "current_action": values.get("proposed_action") or "",
            "current_critique": values.get("critique") or "",
            "frozen_action": values.get("frozen_action") or None,
            "action_freeze_applied": bool(values.get("action_freeze_applied")),
            "iteration_count": int(values.get("iteration_count") or 0),
            "evidence_summary": (values.get("evidence_package") or {}).get("summary") or "",
            "source_ids_cited": values.get("source_ids_cited") or [],
        })
        return

    # Terminal without hitting the gate (e.g., no_response_needed).
    token_usage = values.get("token_usage") or {}
    yield _wrap("run_complete", {
        "stop_reason": stop_reason or "no_response_needed",
        "human_decision": values.get("human_decision") or "",
        "human_feedback": values.get("human_feedback") or "",
        "human_action_override": values.get("human_action_override") or "",
        "final_action": values.get("proposed_action") or "",
        "iteration_count": int(values.get("iteration_count") or 0),
        "total_tokens": _sum_tokens(token_usage),
        "token_usage_by_node": token_usage,
        "retrieval_call_count": 0,  # not captured per-tool-call yet; live-only enhancement later
        "wall_clock_ms": 0,
        "is_demo_override": False,
    })


def _sum_tokens(token_usage: dict[str, Any]) -> int:
    total = 0
    for counts in (token_usage or {}).values():
        if isinstance(counts, dict):
            total += int(counts.get("input", 0)) + int(counts.get("output", 0))
    return total


# ---- Active run registry (in-memory; single-process demo is fine) --------

_ACTIVE_RUNS: dict[str, dict[str, str]] = {}


def register_run(run_id: str, thread_id: str, app_id: str, review_id: str) -> None:
    _ACTIVE_RUNS[run_id] = {
        "thread_id": thread_id,
        "app_id": app_id,
        "review_id": review_id,
        "iteration": 0,  # updated by live_runner as the stream progresses
    }


def lookup_run(run_id: str) -> dict[str, Any] | None:
    return _ACTIVE_RUNS.get(run_id)


def _save_iteration(run_id: str, iteration: int) -> None:
    info = _ACTIVE_RUNS.get(run_id)
    if info is not None:
        info["iteration"] = iteration


def mint_ids() -> tuple[str, str]:
    """Return (run_id, thread_id) as fresh UUIDs."""
    run_id = str(uuid.uuid4())
    thread_id = f"live-{uuid.uuid4()}"
    return run_id, thread_id


def inject_human_decision(
    thread_id: str,
    human_decision: str,
    human_feedback: str = "",
    human_action_override: str = "",
) -> None:
    """Update the paused graph's state with the human's decision."""
    graph = _get_graph()
    graph.update_state(
        _thread_config(thread_id),
        {
            "human_decision": human_decision,
            "human_feedback": human_feedback,
            "human_action_override": human_action_override,
        },
    )


def run_live_resume(run_id: str, thread_id: str) -> Iterator[dict[str, Any]]:
    """Resume a paused graph after human_decision has been injected.

    Emits the same canonical SSE events as run_live. Iteration index continues
    from where run_live paused (stored in _ACTIVE_RUNS).
    """
    graph = _get_graph()
    thread_config = _thread_config(thread_id)
    info = _ACTIVE_RUNS.get(run_id) or {}
    current_iteration = int(info.get("iteration", 0))

    def _wrap(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return envelope(type_, run_id=run_id, iteration=current_iteration, payload=payload)

    try:
        # Passing None to stream continues from the checkpoint.
        for update_event in graph.stream(None, config=thread_config, stream_mode="updates"):
            if not isinstance(update_event, dict):
                continue
            for node_name, updates in update_event.items():
                if node_name.startswith("__"):
                    continue
                if not isinstance(updates, dict):
                    updates = {}

                yield _wrap("node_start", {"node": node_name})

                if node_name == "coordinator":
                    continue

                if node_name == "investigator":
                    ep = updates.get("evidence_package") or {}
                    for step in _retrieval_steps(ep):
                        yield _wrap("retrieval_step", step)

                yield _wrap("state_update", {"node": node_name, "updates": updates})

                # Human rejection → new iteration. Check stop_reason because
                # human_approval_node clears human_decision in its return dict
                # (intentional — see agent/nodes/human_approval.py). stop_reason
                # is set to "revising" on rejection and survives the clear.
                if node_name == "human_approval" and updates.get("stop_reason") == "revising":
                    current_iteration += 1
                # Critic rejection → new iteration ONLY when the rejection will
                # actually trigger a re-draft. Action-only rejections hit the
                # action-freeze shortcut and stay in the same logical iteration.
                if node_name == "critic" and not updates.get("approved", False):
                    reason_type = updates.get("reason_type", "")
                    if reason_type in ("drafting", "evidence"):
                        current_iteration += 1
    except Exception as e:  # noqa: BLE001
        logger.exception("live resume failed")
        yield _wrap("error", {
            "node": "unknown",
            "message": f"{type(e).__name__}: {e}",
            "recoverable": False,
        })
        return

    # Drained — either the graph hit the gate again (revision loop landed at
    # another approval) or it terminated (human_approved / no_response_needed).
    try:
        final_state = graph.get_state(thread_config)
    except Exception as e:  # noqa: BLE001
        yield _wrap("error", {"node": "unknown", "message": f"failed to read final state: {e}", "recoverable": False})
        return

    values = final_state.values or {}
    stop_reason = values.get("stop_reason") or ""

    if stop_reason in ("llm_error", "parse_error"):
        yield _wrap("error", {
            "node": "unknown",
            "message": f"Run terminated with stop_reason={stop_reason}",
            "recoverable": False,
        })
        return

    if _is_at_gate(final_state):
        _save_iteration(run_id, current_iteration)
        yield _wrap("human_gate_open", {
            "current_draft": values.get("drafted_response") or "",
            "current_action": values.get("proposed_action") or "",
            "current_critique": values.get("critique") or "",
            "frozen_action": values.get("frozen_action") or None,
            "action_freeze_applied": bool(values.get("action_freeze_applied")),
            "iteration_count": int(values.get("iteration_count") or 0),
            "evidence_summary": (values.get("evidence_package") or {}).get("summary") or "",
            "source_ids_cited": values.get("source_ids_cited") or [],
        })
        return

    token_usage = values.get("token_usage") or {}
    yield _wrap("run_complete", {
        "stop_reason": stop_reason or "human_approved",
        "human_decision": values.get("human_decision") or "",
        "human_feedback": values.get("human_feedback") or "",
        "human_action_override": values.get("human_action_override") or "",
        "final_action": values.get("proposed_action") or "",
        "iteration_count": int(values.get("iteration_count") or 0),
        "total_tokens": _sum_tokens(token_usage),
        "token_usage_by_node": token_usage,
        "retrieval_call_count": 0,
        "wall_clock_ms": 0,
        "is_demo_override": False,
    })
