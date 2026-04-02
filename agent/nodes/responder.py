"""
agent/nodes/responder.py — Drafts a player-facing response.

Milestone 1: Placeholder that returns a stub response.
Milestone 3: Will use the evidence package, tone matching, and
             feedback memory to draft a real response.
"""

import logging

from agent.state import AgentState

logger = logging.getLogger(__name__)


def responder_node(state: AgentState) -> dict:
    """
    Draft a player-facing response and propose an internal action.

    Milestone 1 (placeholder):
        Returns a stub response and action.

    Milestone 3 (real logic):
        - Loads the draft-response skill
        - Reads evidence package (knows what it can claim and at what confidence)
        - Reads feedback memory examples (dynamic few-shot)
        - Reads critic feedback + revision_reason (if revision cycle)
        - Matches tone to the review
        - Drafts response and proposes internal action
    """
    review = state.get("review_text", "")
    iteration = state.get("iteration_count", 0)
    revision_reason = state.get("revision_reason", "")

    if iteration > 0 and revision_reason:
        logger.info(f"Responder: revision cycle {iteration}, fixing: {revision_reason}")
    else:
        logger.info(f"Responder: drafting initial response")

    return {
        "drafted_response": f"Placeholder response to: {review[:50]}...",
        "proposed_action": "monitor",
        "iteration_count": iteration + 1,
        "node_log": [f"responder: placeholder draft (iteration {iteration + 1})"],
    }