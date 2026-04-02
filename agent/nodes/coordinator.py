"""
Routes the graph based on state fields.
"""

import logging
from agent.state import AgentState

logger = logging.getLogger(__name__)

def coordinator_node(state: AgentState) -> dict:
    """
    Decide what happens next based on current state.

    Returns:
        A dict with any state updates.
    """
    iteration = state.get("iteration_count", 0)
    approved = state.get("approved", False)
    max_iter = state.get("max_iterations", 3)

    logger.info(
        f"Coordinator: iteration={iteration}, approved={approved}, max_iterations={max_iter}"
    )

    # If this is a revision cycle (critic has run), increment the counter
    if iteration > 0 and not approved:
        logger.info(
            f"Coordinator: revision cycle {iteration}, reason: {state.get('revision_reason', 'unknown')}"
        )

    return {
        "node_log": [f"coordinator: iteration={iteration}, approved={approved}"],
    }


def route_from_coordinator(state: AgentState) -> str:
    """
    Conditional edge function: decides where to go after the Coordinator.

    Returns:
        A string key that maps to a node name in the graph's conditional edge configuration.
    """
    approved = state.get("approved", False)
    iteration = state.get("iteration_count", 0)
    max_iter = state.get("max_iterations", 3)

    if approved:
        logger.info("Coordinator: draft approved, end.")
        return "done"

    if iteration >= max_iter:
        logger.warning(
            f"Coordinator: max iterations ({max_iter}) reached, forcing end."
        )
        return "done"

    return "investigate"