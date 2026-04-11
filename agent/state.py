"""
Defines the AgentState TypedDict for the LangGraph agent.
"""

from typing import Annotated, TypedDict
import operator

class AgentState(TypedDict):
    """The shared state object passed between all agent nodes."""

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
    reason_type: str        # Critic's rejection category: "evidence" | "drafting" | "action" | "" (empty when approved)
    retrieval_hint: str     # Critic's suggested search query for the Investigator (only set when reason_type=="evidence"); Investigator clears after use
    stop_reason: str        # Why the graph stopped
    run_id: str             # UUID generated once per run by Coordinator on first entry; threaded through state for audit_log ↔ audit_log_iterations join
    node_log: Annotated[list[str], operator.add]
    token_usage: dict  # Per-node token tracking: {"node_name": {"input": N, "output": N}}

    # Action-freeze state (Iter7: graph-level override for action-only critic rejections)
    # Active freeze (cleared on human rejection — see human_approval.py):
    frozen_action: str          # Currently frozen action value; "" when no freeze is active
    action_freeze_applied: bool # True while an active freeze is in effect
    # Persistent override record (NEVER cleared — survives human rejections for metrics/audit):
    action_override_count: int       # Incremented each time coordinator overrides a critic action rejection
    first_override_at_iteration: int # iteration_count at the moment of the FIRST override; -1 when no override has occurred; never overwritten after first set

    # Human-in-the-loop
    human_decision: str     # "approved", "rejected", or "" (empty = awaiting)
    human_feedback: str     # Free-text feedback from human reviewer
    human_action_override: str  # Optional action swap on approve; empty = no swap. Must be a value in config.PROPOSED_ACTIONS or empty.