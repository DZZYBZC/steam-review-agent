"""
CM (Community Manager) batch orchestrator.

Sequential pipeline over the existing agent graph:

  1. CMPlanner converts a NL goal → structured filter + synthesis_instruction.
  2. SQLite query against reviews ⨝ classifications selects candidates.
  3. For each candidate, drive the existing agent graph via live_runner's
     run_live / run_live_resume, auto-approving at the human_approval gate.
     Every forwarded sub-run event gains `meta_run_id` + `candidate_index`
     inside its payload.
  4. CMSynthesizer produces a markdown rollup.
  5. A final meta `run_complete` reports aggregate counts.

All sub-runs are tagged `is_demo=True` via live_runner.build_initial_state, so
their audit/cluster-note writes skip promotion (see agent/nodes/human_approval
._log_audit: skip_promotion = is_eval or is_demo). CM batches therefore never
feed production few-shot or investigator cluster notes.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Any, Generator, Iterator

import anthropic

from agent.planner_cm import CMPlanner, PlannerResult
from agent.synthesizer_cm import CMSynthesizer
from backend.services import live_runner
from backend.services.sse import envelope
from config import (
    CM_GATE_ALWAYS,
    CM_GATE_COUNT_THRESHOLD,
    CM_GATE_FETCH_TRIGGERED,
    CM_GATE_NO_APP_ID_TRIGGER,
    CM_GATE_ON_UNCERTAIN,
    CM_SUBRUN_PARALLELISM,
    DB_PATH,
)

logger = logging.getLogger(__name__)


# Sub-run events that terminate our per-candidate forwarding loop.
# - human_gate_open: graph paused at HITL; we auto-approve and resume.
# - run_complete: sub-run ended terminally before the gate (e.g.
#   stop_reason="no_response_needed") or after resume.
# - error: sub-run surfaced a terminal error (llm_error, parse_error,
#   or an uncaught exception); no run_complete will follow.
_PRE_GATE_TERMINALS = {"human_gate_open", "run_complete", "error"}
_POST_GATE_TERMINALS = {"run_complete", "error"}


# ---------------------------------------------------------------------------
# CM-level human-confirmation registry (deterministic gate)
# ---------------------------------------------------------------------------
# Module-level map of meta_run_id → {"event": Event, "decision": str}. Filled
# by run_cm when it pauses at the deterministic gate; resolved by
# backend/routes/cm.py:cm_confirm via resolve_pending_confirm().
#
# In-memory only — matches the existing in-process pattern of live_runner's
# human-decision injection. A future multi-worker deployment would need a
# durable store, but for the demo this is sufficient.
_PENDING_CONFIRMS: dict[str, dict[str, Any]] = {}
_PENDING_CONFIRMS_LOCK = threading.Lock()


def register_pending_confirm(meta_run_id: str) -> threading.Event:
    """Register a meta_run_id as awaiting human confirmation, return the Event to wait on."""
    ev = threading.Event()
    with _PENDING_CONFIRMS_LOCK:
        _PENDING_CONFIRMS[meta_run_id] = {"event": ev, "decision": "", "chosen_app_id": ""}
    return ev


def resolve_pending_confirm(meta_run_id: str, decision: str, chosen_app_id: str = "") -> bool:
    """Set the human's decision and (optional) chosen app_id; signal the waiting generator.

    `chosen_app_id` is used when the disambiguation picker is shown — the user's
    pick overrides the planner's filter.app_id before sub-runs fire.
    Returns True on success.
    """
    with _PENDING_CONFIRMS_LOCK:
        entry = _PENDING_CONFIRMS.get(meta_run_id)
        if not entry:
            return False
        entry["decision"] = decision
        entry["chosen_app_id"] = chosen_app_id or ""
        entry["event"].set()
    return True


def _drain_pending_confirm(meta_run_id: str) -> tuple[str, str]:
    """Read the decision + chosen_app_id and clean up. Returns (decision, chosen_app_id)."""
    with _PENDING_CONFIRMS_LOCK:
        entry = _PENDING_CONFIRMS.pop(meta_run_id, None)
    if not entry:
        return ("", "")
    return (entry["decision"], entry["chosen_app_id"])


def run_cm(goal: str, meta_run_id: str) -> Iterator[dict[str, Any]]:
    """Sync generator yielding canonical SSE event envelopes for a CM batch run.

    Event sequence:
      cm_plan_started → cm_plan → cm_candidates
      → for each candidate: cm_candidate_start, N×forwarded-sub-run-events,
                            cm_candidate_complete
      → cm_synthesis → meta run_complete

    Errors from planner / SQL / synthesizer are caught and surfaced as a single
    `error` envelope, after which the generator returns. Per-candidate sub-run
    errors are forwarded as-is and the meta-run continues to the next candidate.
    """
    def wrap(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return envelope(type_, run_id=meta_run_id, iteration=0, payload=payload)

    yield wrap("cm_plan_started", {"goal": goal})

    # --- Step b: planner (ReAct tool-use loop, runs in worker thread) -------
    # We need to interleave the planner's tool-call/result events with the SSE
    # generator's yields. The planner is sync (matches investigator's template),
    # so we drive it from a worker thread that pushes events into a thread-safe
    # queue; the generator yields each item as it arrives. Phase 0A spike
    # verified this pattern flushes through StreamingResponse in real-time.
    try:
        planner_result = yield from _stream_planner(
            goal=goal, meta_run_id=meta_run_id, wrap=wrap,
        )
    except (ValueError, anthropic.APIError) as e:
        logger.exception("CM planner failed")
        yield wrap("error", {
            "node": "cm_planner",
            "message": f"{type(e).__name__}: {e}",
            "recoverable": False,
        })
        return

    # --- Step c: dispatch on planner's terminal action ----------------------
    if planner_result.terminal_action == "reject":
        node = "cm_planner_runaway" if planner_result.runaway else "cm_planner_rejected"
        yield wrap("error", {
            "node": node,
            "message": planner_result.reason,
            "recoverable": False,
        })
        yield wrap("run_complete", {
            "meta_run_id": meta_run_id,
            "total_candidates": 0,
            "total_approved": 0,
            "total_errored": 0,
            "stop_reason": node,
        })
        return

    # planner_result.terminal_action == "execute_batch"
    plan_dict = {
        "filter": planner_result.filter,
        "synthesis_instruction": planner_result.synthesis_instruction,
        "uncertain": planner_result.uncertain,
        "concerns": planner_result.concerns,
        "fetch_calls_made": planner_result.fetch_calls_made,
    }
    yield wrap("cm_plan", {"plan": plan_dict})

    # --- Step c.5: deterministic human gate ---------------------------------
    try:
        actual_count = _count_candidates(planner_result.filter)
    except sqlite3.Error as e:
        logger.exception("CM candidate count failed")
        yield wrap("error", {
            "node": "cm_candidates",
            "message": f"{type(e).__name__}: {e}",
            "recoverable": False,
        })
        return

    gate_triggers = _evaluate_gate_triggers(planner_result, actual_count)
    # If the lookup tool surfaced multiple in-DB matches for the user's typed
    # name, add an explicit disambiguation trigger so the gate fires even if
    # nothing else would. This is independent of CM_GATE_ALWAYS — disambiguation
    # is a hard requirement, not a UX preference.
    if planner_result.app_id_alternatives and len(planner_result.app_id_alternatives) >= 2:
        if "CM_GATE_DISAMBIGUATE_APP" not in gate_triggers:
            gate_triggers.insert(0, "CM_GATE_DISAMBIGUATE_APP")
    if gate_triggers:
        yield wrap("cm_planner_human_gate", {
            "filter": planner_result.filter,
            "synthesis_instruction": planner_result.synthesis_instruction,
            "concerns": planner_result.concerns,
            "gate_triggers": gate_triggers,
            "actual_count": actual_count,
            "app_id_alternatives": planner_result.app_id_alternatives,
        })
        # Park the generator until the human resumes via POST /cm/{meta_run_id}/confirm.
        # The pause is real (Event.wait blocks) — generator cooperatively yields control
        # by simply not yielding. StreamingResponse keeps the connection open.
        ev = register_pending_confirm(meta_run_id)
        ev.wait()  # blocks until cm_confirm fires
        decision, chosen_app_id = _drain_pending_confirm(meta_run_id)
        if decision != "approved":
            yield wrap("error", {
                "node": "cm_planner_rejected",
                "message": f"human rejected at CM-level gate (decision={decision!r})",
                "recoverable": False,
            })
            yield wrap("run_complete", {
                "meta_run_id": meta_run_id,
                "total_candidates": 0,
                "total_approved": 0,
                "total_errored": 0,
                "stop_reason": "cm_gate_rejected",
            })
            return
        # Disambiguation: if the user picked a specific app_id from the alternatives,
        # patch the planner's filter before continuing. Validates that the chosen
        # id is actually in the alternatives list (defense against stale clients).
        if chosen_app_id and planner_result.app_id_alternatives:
            valid_ids = {alt["app_id"] for alt in planner_result.app_id_alternatives}
            if chosen_app_id in valid_ids:
                planner_result.filter["app_id"] = chosen_app_id
                logger.info(
                    "CM run %s: human picked app_id=%s from %d alternatives",
                    meta_run_id, chosen_app_id, len(valid_ids),
                )
            else:
                logger.warning(
                    "CM run %s: chosen_app_id=%s not in alternatives %s; ignoring",
                    meta_run_id, chosen_app_id, sorted(valid_ids),
                )

    # --- Step d: candidate selection (existing path, unchanged below) -------
    try:
        candidates = _select_candidates(planner_result.filter)
    except sqlite3.Error as e:
        logger.exception("CM candidate query failed")
        yield wrap("error", {
            "node": "cm_candidates",
            "message": f"{type(e).__name__}: {e}",
            "recoverable": False,
        })
        return

    yield wrap("cm_candidates", {"candidates": [
        {
            "app_id": c["app_id"],
            "review_id": c["review_id"],
            "category": c["primary_category"],
            "review_preview": (c["review_text"] or "")[:160],
        }
        for c in candidates
    ]})

    # The downstream candidate loop and synthesizer expect a `plan` dict shaped
    # like the legacy planner output (filter + synthesis_instruction). Build it.
    plan = {
        "filter": planner_result.filter,
        "strategy": "triage_each",
        "synthesis_instruction": planner_result.synthesis_instruction,
    }

    # --- Step d: per-candidate PARALLEL loop --------------------------------
    # Each sub-run is independent (unique sub_run_id, sub_thread_id, LangGraph
    # checkpoint thread). We run up to CM_SUBRUN_PARALLELISM concurrently via
    # a ThreadPoolExecutor; each worker streams events directly into a shared
    # queue as they arrive (real-time, not batch-on-completion). The generator
    # drains the queue and yields events as fast as they land — frontend cards
    # advance in parallel rather than one-at-a-time.
    candidate_results: list[Any] = [None] * len(candidates)
    approved_count = 0
    errored_count = 0
    sub_q: queue.Queue = queue.Queue()
    _COMPLETE = object()  # sentinel marking one worker's completion

    # Emit all cm_candidate_start events upfront so the frontend renders all
    # candidate cards before any sub-run progress arrives. Cards advance in
    # parallel as their workers fire forwarded events.
    for i, cand in enumerate(candidates):
        yield wrap("cm_candidate_start", {
            "candidate_index": i,
            "review_id": cand["review_id"],
            "app_id": cand["app_id"],
        })

    def worker(idx: int, cand: dict[str, Any]) -> None:
        try:
            result = _drive_sub_run_streaming(
                app_id=cand["app_id"],
                review_id=cand["review_id"],
                category=cand["primary_category"],
                meta_run_id=meta_run_id,
                candidate_index=idx,
                event_queue=sub_q,
            )
        except Exception as e:  # noqa: BLE001 — surface any unexpected orchestration error
            logger.exception("CM sub-run orchestration failed for candidate %d", idx)
            sub_q.put(("event", envelope("error", run_id=meta_run_id, iteration=0, payload={
                "node": "cm_sub_run",
                "message": f"orchestration error: {type(e).__name__}: {e}",
                "recoverable": True,
                "candidate_index": idx,
                "review_id": cand["review_id"],
            })))
            result = {
                "review_id": cand["review_id"],
                "app_id": cand["app_id"],
                "category": cand["primary_category"],
                "stop_reason": "error",
                "critic_approved": None,
                "proposed_action": "",
                "drafted_response": "",
                "evidence_confidence": None,
                "error_message": f"{type(e).__name__}: {e}",
            }
        sub_q.put(("complete", idx, result))

    workers_done = 0
    n_total = len(candidates)
    if n_total > 0:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(CM_SUBRUN_PARALLELISM, n_total),
            thread_name_prefix="cm_subrun",
        )
        try:
            for i, cand in enumerate(candidates):
                executor.submit(worker, i, cand)

            while workers_done < n_total:
                item = sub_q.get()
                kind = item[0]
                if kind == "event":
                    yield item[1]
                else:  # "complete"
                    _, idx, result = item
                    candidate_results[idx] = result
                    if result["stop_reason"] == "human_approved":
                        approved_count += 1
                    if result["stop_reason"] == "error":
                        errored_count += 1
                    yield wrap("cm_candidate_complete", {
                        "candidate_index": idx,
                        "review_id": result["review_id"],
                        "stop_reason": result["stop_reason"],
                        "critic_approved": result["critic_approved"],
                        "proposed_action": result["proposed_action"],
                        "evidence_confidence": result["evidence_confidence"],
                        "drafted_response": result["drafted_response"] or "",
                        "error_message": result["error_message"],
                    })
                    workers_done += 1
        finally:
            executor.shutdown(wait=True)

    # Filter out any None entries (shouldn't happen — every worker pushes either
    # success or error result — but guard against it for the synthesizer).
    candidate_results = [r for r in candidate_results if r is not None]

    # --- Step e: synthesis --------------------------------------------------
    if not candidate_results:
        # Zero-candidate path: deterministic markdown stub, no Haiku call.
        plan_summary = _summarize_plan_for_empty(plan)
        markdown_str = (
            f"## Pattern\n\nNo reviews matched the filter: {plan_summary}.\n\n"
            f"## Recommended escalations\n\n- _(none)_\n\n"
            f"## Per-review drafts\n\n- _(no approved drafts in this batch)_\n"
        )
    else:
        try:
            markdown_str = CMSynthesizer().synthesize(
                goal=goal, plan=plan, candidates=candidate_results,
            )
        except (ValueError, anthropic.APIError) as e:
            logger.exception("CM synthesizer failed")
            yield wrap("error", {
                "node": "cm_synthesizer",
                "message": f"{type(e).__name__}: {e}",
                "recoverable": False,
            })
            return

    yield wrap("cm_synthesis", {"markdown": markdown_str})

    # --- Step f: meta run_complete -----------------------------------------
    # Second run_complete in the stream per candidate: each sub-run emits its own
    # (run_id = sub_run_id, payload.meta_run_id set by our wrapper), and this is
    # the CM-level one (run_id = meta_run_id). Client disambiguates by run_id.
    yield wrap("run_complete", {
        "meta_run_id": meta_run_id,
        "total_candidates": len(candidate_results),
        "total_approved": approved_count,
        "total_errored": errored_count,
    })


# ---------------------------------------------------------------------------
# Planner streaming wrapper
# ---------------------------------------------------------------------------

_PLANNER_DONE_SENTINEL = object()


def _stream_planner(
    goal: str,
    meta_run_id: str,
    wrap: Any,
) -> Generator[dict[str, Any], None, PlannerResult]:
    """Run the CM planner in a worker thread and yield its activity events.

    Uses the thread+queue pattern verified by the Phase 0A spike — the planner
    is a sync while-loop (matching the investigator's template); we drive it
    from a thread so its emit_event callback can push events into a queue while
    the SSE generator yields them in real-time.

    Returns the final PlannerResult via the generator's StopIteration.value
    (callers use `yield from _stream_planner(...)` and assign the return value).
    """
    q: queue.Queue = queue.Queue()
    result_holder: dict[str, Any] = {}

    def emit(event_type: str, payload: dict) -> None:
        # Always include meta_run_id in the payload so the frontend can
        # disambiguate planner activity for this run from any concurrent runs.
        payload = dict(payload)
        payload["meta_run_id"] = meta_run_id
        q.put(wrap(event_type, payload))

    def worker() -> None:
        try:
            result_holder["result"] = CMPlanner().plan(
                goal=goal,
                current_date=date.today().isoformat(),
                run_id=meta_run_id,
                event_emitter=emit,
            )
        except Exception as e:  # noqa: BLE001 — surface to caller via holder
            result_holder["error"] = e
        finally:
            q.put(_PLANNER_DONE_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = q.get()
        if item is _PLANNER_DONE_SENTINEL:
            break
        yield item

    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder["result"]


# ---------------------------------------------------------------------------
# Deterministic CM-level human gate
# ---------------------------------------------------------------------------

def _count_candidates(filter_dict: dict[str, Any]) -> int:
    """Cheap SQL count for the gate's COUNT_THRESHOLD check.

    Mirrors _select_candidates' WHERE clauses but returns COUNT(*). Note that
    this excludes the de-novo-only case (al.review_id IS NULL OR al.is_demo=1)
    too — gating decisions should be on the same population the candidate loop
    will draw from.
    """
    clauses = [
        "(al.review_id IS NULL OR al.is_demo = 1)",
        "r.is_near_duplicate = 0",
        "LENGTH(r.review_text) >= 40",
    ]
    params: list[Any] = []
    if filter_dict.get("app_id"):
        clauses.append("r.app_id = ?")
        params.append(filter_dict["app_id"])
    if filter_dict.get("category"):
        clauses.append("c.primary_category = ?")
        params.append(filter_dict["category"])
    if filter_dict.get("voted_up") is not None:
        clauses.append("r.voted_up = ?")
        params.append(int(bool(filter_dict["voted_up"])))
    if filter_dict.get("since_days") is not None:
        cutoff = (datetime.utcnow() - timedelta(days=int(filter_dict["since_days"]))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        clauses.append("r.timestamp >= ?")
        params.append(cutoff)

    sql = f"""
        SELECT COUNT(*)
        FROM reviews r
        INNER JOIN classifications c ON r.review_id = c.review_id
        LEFT JOIN audit_log al ON r.review_id = al.review_id
        WHERE {" AND ".join(clauses)}
    """
    with sqlite3.connect(DB_PATH) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _evaluate_gate_triggers(
    planner_result: PlannerResult, actual_count: int,
) -> list[str]:
    """Apply the deterministic gate rules. Returns a list of trigger names that fired.

    Empty list = no gate (auto-execute). Non-empty = pause for human confirmation.
    """
    triggers: list[str] = []
    # READY_TO_DRAFT fires unconditionally when CM_GATE_ALWAYS is set — turns the
    # gate into a universal "ready to draft, approve to proceed" confirmation step
    # rather than an exception path. Listed first so it shows as the primary
    # reason in the UI; the more specific triggers below clarify *why* this
    # particular run might warrant extra scrutiny.
    if CM_GATE_ALWAYS:
        triggers.append("READY_TO_DRAFT")
    if CM_GATE_ON_UNCERTAIN and (planner_result.uncertain or len(planner_result.concerns) > 0):
        triggers.append("CM_GATE_ON_UNCERTAIN")
    if CM_GATE_NO_APP_ID_TRIGGER and not (planner_result.filter or {}).get("app_id"):
        triggers.append("CM_GATE_NO_APP_ID_TRIGGER")
    if CM_GATE_FETCH_TRIGGERED and any((planner_result.fetch_calls_made or {}).values()):
        triggers.append("CM_GATE_FETCH_TRIGGERED")
    if actual_count > CM_GATE_COUNT_THRESHOLD:
        triggers.append("CM_GATE_COUNT_THRESHOLD")
    return triggers


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _select_candidates(filter_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the candidate query. Returns a list of row dicts."""
    # (al.review_id IS NULL OR al.is_demo = 1) — excludes reviews that already
    # have a REAL (non-demo) audit entry, but permits re-picking reviews that
    # only have demo sub-runs. Without this, repeated CM runs would quickly
    # exhaust the candidate pool even though none of those runs promoted.
    clauses = [
        "(al.review_id IS NULL OR al.is_demo = 1)",
        "r.is_near_duplicate = 0",
        "LENGTH(r.review_text) >= 40",
    ]
    params: list[Any] = []

    if filter_dict.get("app_id"):
        clauses.append("r.app_id = ?")
        params.append(filter_dict["app_id"])
    if filter_dict.get("category"):
        clauses.append("c.primary_category = ?")
        params.append(filter_dict["category"])
    if filter_dict.get("voted_up") is not None:
        clauses.append("r.voted_up = ?")
        # SQLite stores bool as 0/1; planner emits JSON bool.
        params.append(int(bool(filter_dict["voted_up"])))
    if filter_dict.get("since_days") is not None:
        # pipeline/storage.save_reviews stores timestamp as str(pd.to_datetime(..., unit="s")),
        # which renders as "YYYY-MM-DD HH:MM:SS". Lexical compare matches chronological order.
        cutoff = (datetime.utcnow() - timedelta(days=int(filter_dict["since_days"]))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        clauses.append("r.timestamp >= ?")
        params.append(cutoff)

    limit = int(filter_dict.get("limit", 5))
    params.append(limit)

    sql = f"""
        SELECT r.review_id, r.app_id, r.review_text, r.voted_up, r.timestamp,
               c.primary_category
        FROM reviews r
        INNER JOIN classifications c ON r.review_id = c.review_id
        LEFT JOIN audit_log al ON r.review_id = al.review_id
        WHERE {" AND ".join(clauses)}
        ORDER BY r.timestamp DESC
        LIMIT ?
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Sub-run driver
# ---------------------------------------------------------------------------

def _drive_sub_run_streaming(
    app_id: str,
    review_id: str,
    category: str,
    meta_run_id: str,
    candidate_index: int,
    event_queue: queue.Queue,
) -> dict[str, Any]:
    """Run the agent graph for one candidate, pushing each forwarded event
    into `event_queue` as it arrives.

    Auto-approves at the human_approval interrupt — the per-sub-run gate is
    invisible to the user; the CM-level gate in run_cm is the only user-visible
    confirmation (load-bearing two-gate contract). All sub-run writes are
    tagged is_demo=True by live_runner.build_initial_state, so they never enter
    the production promoted pool.

    Used by the ThreadPoolExecutor workers in run_cm so the SSE generator yields
    events from all parallel sub-runs as they happen, rather than batch-on-completion.
    """
    sub_run_id, sub_thread_id = live_runner.mint_ids()
    live_runner.register_run(sub_run_id, sub_thread_id, app_id, review_id)

    captured = {
        "stop_reason": None,
        "critic_approved": None,
        "proposed_action": "",
        "drafted_response": "",
        "evidence_confidence": None,
        "error_message": None,
    }

    hit_gate = False
    for ev in live_runner.run_live(
        app_id=app_id, review_id=review_id,
        run_id=sub_run_id, thread_id=sub_thread_id,
    ):
        enriched = _enrich_event(ev, meta_run_id=meta_run_id, candidate_index=candidate_index)
        event_queue.put(("event", enriched))
        _capture_from_event(ev, captured)
        if ev["type"] in _PRE_GATE_TERMINALS:
            if ev["type"] == "human_gate_open":
                hit_gate = True
            break

    if hit_gate:
        live_runner.inject_human_decision(
            thread_id=sub_thread_id,
            human_decision="approved",
            human_feedback="",
            human_action_override="",
        )
        for ev in live_runner.run_live_resume(run_id=sub_run_id, thread_id=sub_thread_id):
            enriched = _enrich_event(ev, meta_run_id=meta_run_id, candidate_index=candidate_index)
            event_queue.put(("event", enriched))
            _capture_from_event(ev, captured)
            if ev["type"] in _POST_GATE_TERMINALS:
                break

    if captured["stop_reason"] is None:
        captured["stop_reason"] = "unknown"

    return {
        "review_id": review_id,
        "app_id": app_id,
        "category": category,
        **captured,
    }


def _enrich_event(
    ev: dict[str, Any], meta_run_id: str, candidate_index: int,
) -> dict[str, Any]:
    """Shallow-copy the event and stamp meta_run_id + candidate_index into its payload.

    The canonical envelope schema (type, run_id, iteration, timestamp, payload)
    is preserved — only payload gets extra keys.
    """
    out = dict(ev)
    payload = dict(out.get("payload") or {})
    payload["meta_run_id"] = meta_run_id
    payload["candidate_index"] = candidate_index
    out["payload"] = payload
    return out


def _capture_from_event(ev: dict[str, Any], captured: dict[str, Any]) -> None:
    """Update the running capture dict from a forwarded event.

    Reads from the ORIGINAL event's payload (pre-enrichment) — the live_runner
    emits the same keys live_runner uses itself, so we rely on its stable
    payload shape for state_update / human_gate_open / run_complete / error.
    """
    etype = ev.get("type")
    payload = ev.get("payload") or {}

    if etype == "state_update":
        node = payload.get("node")
        updates = payload.get("updates") or {}

        if node == "critic" and "approved" in updates:
            captured["critic_approved"] = bool(updates.get("approved"))

        if "drafted_response" in updates and updates["drafted_response"]:
            captured["drafted_response"] = updates["drafted_response"]
        if "proposed_action" in updates and updates["proposed_action"]:
            captured["proposed_action"] = updates["proposed_action"]
        if "evidence_package" in updates:
            ep = updates["evidence_package"] or {}
            if "confidence" in ep:
                # may be a float or None; keep as-is
                captured["evidence_confidence"] = ep.get("confidence")

    elif etype == "human_gate_open":
        # Best-effort capture from the gate payload — these are the values
        # live_runner flushed at the gate, which matches what the critic emitted.
        if payload.get("current_draft") and not captured["drafted_response"]:
            captured["drafted_response"] = payload["current_draft"]
        if payload.get("current_action") and not captured["proposed_action"]:
            captured["proposed_action"] = payload["current_action"]

    elif etype == "run_complete":
        stop_reason = payload.get("stop_reason") or captured["stop_reason"] or "unknown"
        captured["stop_reason"] = stop_reason
        # run_complete carries final_action; prefer it over the incremental capture
        # only if we don't already have one (the graph may have overridden actions post-critic).
        final_action = payload.get("final_action")
        if final_action:
            captured["proposed_action"] = final_action

    elif etype == "error":
        captured["stop_reason"] = "error"
        captured["error_message"] = payload.get("message") or "unknown error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarize_plan_for_empty(plan: dict[str, Any]) -> str:
    """Short human-readable plan summary for the zero-candidate markdown stub."""
    parts = []
    f = plan.get("filter", {})
    if f.get("app_id"):
        parts.append(f"app={f['app_id']}")
    if f.get("category"):
        parts.append(f"category={f['category']}")
    if f.get("voted_up") is not None:
        parts.append(f"voted_up={f['voted_up']}")
    if f.get("since_days") is not None:
        parts.append(f"since_days={f['since_days']}")
    parts.append(f"limit={f.get('limit', 5)}")
    return ", ".join(parts) or "no filters"
