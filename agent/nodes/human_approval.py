"""
Human-in-the-loop approval gate.

Reads the human decision from state and routes accordingly.
The graph interrupts before this node so the caller can inject
human_decision and human_feedback into state before resuming.

Plain Python — no LLM call.
"""

import logging
from agent.state import AgentState
from pipeline.storage import (
    get_connection, save_audit_entry, save_cluster_note,
    find_recent_similar_note, update_cluster_note_text,
)

logger = logging.getLogger(__name__)


def _log_audit(state: AgentState, stop_reason: str) -> None:
    """Save audit entry and cluster notes based on the human decision."""
    decision = state.get("human_decision", "")
    feedback = state.get("human_feedback", "")
    app_id = state.get("app_id", "")
    category = state.get("cluster_summary", {}).get("category", "")
    review_id = state.get("review_id", "")

    # Merge the final stop_reason so the audit entry reflects the node's decision
    audit_state = {**state, "stop_reason": stop_reason}

    conn = get_connection()
    try:
        save_audit_entry(conn, audit_state)

        if not app_id or not category:
            return

        if decision == "rejected" and feedback:
            # Dedup: check for existing note from same review or recent window
            existing = find_recent_similar_note(
                conn, app_id, category, "human_feedback",
                source_review_id=review_id,
            )
            if existing:
                update_cluster_note_text(conn, existing["id"], feedback)
            else:
                save_cluster_note(
                    conn, app_id=app_id, category=category,
                    note_type="human_feedback", note_text=feedback,
                    created_by="human", source_review_id=review_id,
                )

        elif decision == "approved":
            # Save response history for future investigator context
            drafted = state.get("drafted_response", "")
            action = state.get("proposed_action", "")
            note_text = f"[action={action}] {drafted}"

            existing = find_recent_similar_note(
                conn, app_id, category, "response_history",
                source_review_id=review_id,
            )
            if not existing:
                save_cluster_note(
                    conn, app_id=app_id, category=category,
                    note_type="response_history", note_text=note_text,
                    created_by="system", source_review_id=review_id,
                )

    except Exception as e:
        logger.error(f"Failed to save audit/cluster note: {e}")
    finally:
        conn.close()


def human_approval_node(state: AgentState) -> dict:
    """
    Process the human reviewer's decision.

    Returns state updates based on human_decision:
    - "approved": end the graph with human_approved stop reason
    - "rejected": re-enter revision loop with human feedback
    - "" / missing: awaiting review (interrupt should have stopped us)
    """
    decision = state.get("human_decision", "")
    feedback = state.get("human_feedback", "")

    if decision == "approved":
        logger.info("Human approved the draft.")
        _log_audit(state, "human_approved")
        return {
            "stop_reason": "human_approved",
            "node_log": ["human_approval: human approved the draft"],
        }

    if decision == "rejected":
        reason = feedback or "Human rejected without feedback"
        logger.info(f"Human rejected the draft: {reason}")
        _log_audit(state, "revising")
        return {
            "approved": False,
            "revision_reason": reason,
            "stop_reason": "revising",
            "node_log": [f"human_approval: human rejected — {reason}"],
        }

    # Empty decision — graph interrupted before we got here, or resume without decision
    logger.info("Human approval gate: awaiting decision.")
    return {
        "stop_reason": "awaiting_human_review",
        "node_log": ["human_approval: awaiting human decision"],
    }


def route_from_human_approval(state: AgentState) -> str:
    """
    Conditional edge: decide where to go after the human approval gate.

    Returns:
        "done" if approved or awaiting (interrupt handles the wait),
        "coordinator" if rejected (re-enters revision loop).
    """
    decision = state.get("human_decision", "")

    if decision == "rejected":
        return "coordinator"

    # "approved" or "" — both go to END
    return "done"
