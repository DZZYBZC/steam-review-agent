"""
agent/nodes/critic.py — Reviews the drafted response for quality.

Milestone 1: Placeholder that auto-approves.
Milestone 3: Will check evidence alignment, hallucination, tone,
             and source_id coverage.
"""

import logging

from agent.state import AgentState

logger = logging.getLogger(__name__)


def critic_node(state: AgentState) -> dict:
    """
    Evaluate the drafted response against the evidence package.

    Milestone 1 (placeholder):
        Auto-approves everything so we can test the graph flow.

    Milestone 3 (real logic):
        - Loads the critique-draft skill
        - Reads evidence package (checks source_ids and confidence levels)
        - Reads drafted_response (checks every claim has evidence)
        - Reads proposed_action (checks it's appropriate)
        - Checks tone matches the review tone
        - Approves or rejects with a specific revision_reason
    """
    draft = state.get("drafted_response", "")
    logger.info(f"Critic: evaluating draft ({len(draft)} chars)")

    # Placeholder: auto-approve so the graph completes
    return {
        "critique": "Placeholder critique — auto-approved.",
        "approved": True,
        "revision_reason": "",
        "stop_reason": "approved",
        "node_log": ["critic: placeholder — auto-approved"],
    }