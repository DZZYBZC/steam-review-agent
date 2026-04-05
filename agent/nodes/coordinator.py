"""
Routes the graph based on state fields.
"""

import logging
from agent.state import AgentState
from config import AGENT_MAX_ITERATIONS

logger = logging.getLogger(__name__)

def coordinator_node(state: AgentState) -> dict:
    """
    Decide what happens next based on current state.

    Returns:
        A dict with any state updates.
    """
    iteration = state.get("iteration_count", 0)
    approved = state.get("approved", False)

    logger.info(
        f"Coordinator: iteration={iteration}, approved={approved}, max_iterations={AGENT_MAX_ITERATIONS}"
    )

    if iteration > 0 and not approved:
        logger.info(
            f"Coordinator: revision cycle {iteration}, reason: {state.get('revision_reason', 'unknown')}"
        )

    if approved:
        return {
            "stop_reason": "approved",
            "node_log": [f"coordinator: iteration={iteration}, approved — ending"],
        }

    if iteration >= AGENT_MAX_ITERATIONS:
        return {
            "stop_reason": "max_iterations_reached",
            "node_log": [f"coordinator: iteration={iteration}, max iterations reached — ending"],
        }

    return {
        "stop_reason": "revising",
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

    if approved:
        logger.info("Coordinator: draft approved, end.")
        return "done"

    if iteration >= AGENT_MAX_ITERATIONS:
        logger.warning(
            f"Coordinator: max iterations ({AGENT_MAX_ITERATIONS}) reached, forcing end."
        )
        return "done"

    if iteration == 0:
        return "investigate"

    return "respond"
