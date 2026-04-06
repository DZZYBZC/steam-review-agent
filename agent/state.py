"""
Defines the AgentState TypedDict for the LangGraph agent.
"""

from typing import Annotated, TypedDict
import operator

class AgentState(TypedDict):
    """
    The shared state object passed between all agent nodes.

    Fields are grouped by purpose:
    - Input: data that enters the graph at the start
    - Node outputs: results produced by each node
    - Control flow: fields that govern routing and termination
    """

    # Input
    app_id: str
    review_id: str
    review_text: str
    cluster_summary: dict # cluster.py
    review_tone: str

    # Output
    evidence_package: dict  # Investigator
    drafted_response: str   # Responder
    proposed_action: str    # Responder
    source_ids_cited: list[str]  # Responder — chunk ids cited in the draft
    critique: str           # Critic (Reflection)

    # Control flow
    iteration_count: int
    approved: bool          # Whether the Critic approved the draft
    revision_reason: str    # Why the Critic rejected the draft (empty string if approved)
    reason_type: str        # Critic's rejection category: "evidence" | "drafting" | "" (empty when approved)
    retrieval_hint: str     # Critic's suggested search query for the Investigator (only set when reason_type=="evidence"); Investigator clears after use
    stop_reason: str        # Why the graph stopped
    node_log: Annotated[list[str], operator.add]
    token_usage: dict  # Per-node token tracking: {"node_name": {"input": N, "output": N}}

    # Human-in-the-loop
    human_decision: str     # "approved", "rejected", or "" (empty = awaiting)
    human_feedback: str     # Free-text feedback from human reviewer