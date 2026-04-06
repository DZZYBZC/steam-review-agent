"""
Routes the graph based on state fields.
"""

import logging
import uuid
from agent.state import AgentState
from pipeline.storage import get_connection, save_audit_entry
from config import AGENT_MAX_ITERATIONS

logger = logging.getLogger(__name__)

TERMINAL_ERROR_STOP_REASONS = {"llm_error", "parse_error"}

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

    # Terminal error from Responder or Critic — audit-log and end.
    if stop_reason in TERMINAL_ERROR_STOP_REASONS:
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
            "node_log": [f"coordinator: terminal error stop_reason={stop_reason} — ending"],
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

    if approved:
        return {
            "run_id": run_id,
            "stop_reason": "approved",
            "node_log": [f"coordinator: iteration={iteration}, approved — ending"],
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
        "node_log": [f"coordinator: iteration={iteration}, routing next"],
    }


def route_from_coordinator(state: AgentState) -> str:
    """
    Conditional edge function: decides where to go after the Coordinator.

    Returns:
        A string key that maps to a node name in the graph's conditional edge configuration.
    """
    approved = state.get("approved", False)
    iteration = state.get("iteration_count", 0)
    stop_reason = state.get("stop_reason", "")

    if stop_reason in TERMINAL_ERROR_STOP_REASONS:
        logger.warning(f"Coordinator: terminal error ({stop_reason}), ending.")
        return "done"

    if approved:
        logger.info("Coordinator: draft approved, end.")
        return "done"

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
