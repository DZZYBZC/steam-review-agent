"""
Routes the graph based on state fields.
"""

import logging
import uuid
from agent.state import AgentState
from pipeline.storage import get_connection, save_audit_entry
from config import (
    AGENT_MAX_ITERATIONS,
    ACTION_FREEZE_ENABLED,
    ACTION_FREEZE_FILTER_ENABLED,
    ACTION_FREEZE_MIN_PROXIMITY,
    PROPOSED_ACTIONS,
)

logger = logging.getLogger(__name__)

TERMINAL_ERROR_STOP_REASONS = {"llm_error", "parse_error"}
TERMINAL_NORMAL_STOP_REASONS = {"no_response_needed"}


def _should_freeze(responder_action: str, critic_pref: str) -> bool:
    """
    Decide whether a critic action-only rejection warrants the freeze path.

    Freeze when the critic's preferred action is either more severe than the
    responder's (bump-up) OR at least ACTION_FREEZE_MIN_PROXIMITY rungs away.
    Bump-down-by-1 critiques are dropped as noise (83% R-right in baseline)
    and routed to the approve-anyway branch instead.

    Conservative fallback — returns True when either action label is missing or
    unknown, preserving pre-filter behavior.
    """
    if not ACTION_FREEZE_FILTER_ENABLED:
        return True
    if responder_action not in PROPOSED_ACTIONS or critic_pref not in PROPOSED_ACTIONS:
        return True
    resp_idx = PROPOSED_ACTIONS.index(responder_action)
    crit_idx = PROPOSED_ACTIONS.index(critic_pref)
    diff = crit_idx - resp_idx
    return diff > 0 or abs(diff) >= ACTION_FREEZE_MIN_PROXIMITY

def coordinator_node(state: AgentState) -> dict:
    """
    Decide what happens next based on current state.

    Returns:
        A dict with any state updates.
    """
    iteration = state.get("iteration_count", 0)
    approved = state.get("approved", False)
    stop_reason = state.get("stop_reason", "")
    # Mint a run_id on first entry; subsequent entries pass the existing one through.
    run_id = state.get("run_id", "") or str(uuid.uuid4())

    logger.info(
        f"Coordinator: iteration={iteration}, approved={approved}, stop_reason={stop_reason!r}, max_iterations={AGENT_MAX_ITERATIONS}"
    )

    # Terminal stop from Responder/Critic (error) or Investigator (skip path) — audit-log and end.
    if stop_reason in TERMINAL_ERROR_STOP_REASONS or stop_reason in TERMINAL_NORMAL_STOP_REASONS:
        try:
            conn = get_connection()
            try:
                save_audit_entry(conn, {**state, "run_id": run_id})
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Coordinator: failed to save audit entry on {stop_reason}: {e}")
        return {
            "run_id": run_id,
            "node_log": [f"coordinator: terminal stop_reason={stop_reason} — ending"],
        }

    human_decision = state.get("human_decision", "")

    if human_decision == "rejected":
        logger.info(
            f"Coordinator: human rejected at iteration {iteration}, reason: {state.get('revision_reason', 'unknown')}"
        )
    elif iteration > 0 and not approved:
        logger.info(
            f"Coordinator: critic revision cycle {iteration}, reason: {state.get('revision_reason', 'unknown')}"
        )

    # Action-freeze interception: if critic rejected on action grounds only,
    # override the rejection and route to human. Checked BEFORE max_iterations
    # so the override fires even at the iteration cap.
    # Gated on ACTION_FREEZE_ENABLED (Iter 8 alt 1: disabled — action-only rejections
    # now flow through the normal revision loop; critic-right/responder-wrong cases
    # get a chance to land ideal via redraft instead of terminating at HITL).
    # ACTION_FREEZE_FILTER_ENABLED narrows this into two paths based on the
    # critic's preferred action (see _should_freeze): the full freeze path for
    # signal-likely rejections, or an approve-anyway path for bump-down-by-1
    # rejections that carry an 83% R-right noise rate in baseline data.
    reason_type = state.get("reason_type", "")
    if ACTION_FREEZE_ENABLED and not approved and reason_type == "action" and human_decision != "rejected":
        action = state.get("proposed_action", "monitor")
        critic_pref = state.get("critic_preferred_action", "") or ""
        freeze_this = _should_freeze(action, critic_pref)

        if freeze_this:
            frozen = state.get("frozen_action", "") or action
            logger.info(
                f"Coordinator: action-only rejection overridden, "
                f"freezing action at '{frozen}' (critic_pref='{critic_pref or 'n/a'}')"
            )
            override_count = state.get("action_override_count", 0) + 1
            # Record the iteration of the FIRST override only (never overwritten).
            # Convention: uses iteration_count, which is 1 after the first
            # investigate→respond→critic pass. So Spot 1's expected value is 1.
            first_override_iter = state.get("first_override_at_iteration", -1)
            if first_override_iter < 0:
                first_override_iter = iteration
            return {
                "run_id": run_id,
                "frozen_action": frozen,
                "action_freeze_applied": True,
                "action_override_count": override_count,
                "first_override_at_iteration": first_override_iter,
                "proposed_action": frozen,
                "approved": True,
                "revision_reason": "",
                "reason_type": "",
                "stop_reason": "action_override",
                "human_decision": "",
                "human_feedback": "",
                "human_action_override": "",
                "node_log": [
                    f"coordinator: action-only rejection overridden, "
                    f"frozen_action={frozen}, override_count={override_count}, "
                    f"routing to human_approval"
                ],
            }

        # Approve-anyway: filter judged critic's bump-down-by-1 as noise. Accept
        # responder's action as-is, no freeze metadata, route to human_approval.
        skip_count = state.get("action_filter_skip_count", 0) + 1
        logger.info(
            f"Coordinator: action-only rejection filter-skipped "
            f"(responder='{action}', critic_pref='{critic_pref}'), "
            f"approving responder's action"
        )
        return {
            "run_id": run_id,
            "action_filter_skip_count": skip_count,
            "approved": True,
            "revision_reason": "",
            "reason_type": "",
            "stop_reason": "action_filter_skip",
            "human_decision": "",
            "human_feedback": "",
            "human_action_override": "",
            "node_log": [
                f"coordinator: action-only rejection filter-skipped "
                f"(responder='{action}', critic_pref='{critic_pref}'), "
                f"skip_count={skip_count}, routing to human_approval"
            ],
        }

    if iteration >= AGENT_MAX_ITERATIONS:
        stop_reason = "max_iterations_reached"
        try:
            conn = get_connection()
            try:
                save_audit_entry(conn, {**state, "run_id": run_id, "stop_reason": stop_reason})
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Coordinator: failed to save audit entry on max_iterations: {e}")
        return {
            "run_id": run_id,
            "stop_reason": stop_reason,
            "node_log": [f"coordinator: iteration={iteration}, max iterations reached — ending"],
        }

    return {
        "run_id": run_id,
        "stop_reason": "revising",
        "human_decision": "",
        "human_feedback": "",
        "human_action_override": "",
        "node_log": [f"coordinator: iteration={iteration}, routing next"],
    }


def route_from_coordinator(state: AgentState) -> str:
    """
    Conditional edge function: decides where to go after the Coordinator.

    Returns:
        A string key that maps to a node name in the graph's conditional edge configuration.
    """
    iteration = state.get("iteration_count", 0)
    stop_reason = state.get("stop_reason", "")

    if stop_reason in TERMINAL_ERROR_STOP_REASONS or stop_reason in TERMINAL_NORMAL_STOP_REASONS:
        logger.info(f"Coordinator: terminal stop_reason ({stop_reason}), ending.")
        return "done"

    # Action-freeze override (or filter-skip): coordinator overrode a critic
    # action-only rejection. Checked BEFORE max_iterations so the override fires
    # even at the iteration cap. Both paths route to human_approval; only the
    # freeze metadata differs.
    if stop_reason in ("action_override", "action_filter_skip"):
        return "human_approval"

    if iteration >= AGENT_MAX_ITERATIONS:
        logger.warning(
            f"Coordinator: max iterations ({AGENT_MAX_ITERATIONS}) reached, forcing end."
        )
        return "done"

    # First pass: always investigate.
    if iteration == 0:
        return "investigate"

    # Revision cycles: if the Critic flagged an evidence gap, re-investigate.
    # Otherwise (drafting-type rejection, or unclassified), re-draft only.
    if state.get("reason_type", "") == "evidence":
        logger.info("Coordinator: evidence-type rejection, routing back to investigator")
        return "investigate"

    return "respond"
