"""
Constructs and compiles the LangGraph StateGraph.
"""

import logging
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from agent.state import AgentState
from agent.nodes.coordinator import coordinator_node, route_from_coordinator
from agent.nodes.investigator import investigator_node
from agent.nodes.responder import responder_node
from agent.nodes.critic import critic_node
from agent.nodes.human_approval import human_approval_node, route_from_human_approval
from config import CHECKPOINT_BACKEND, CHECKPOINT_DB_PATH

TERMINAL_ERROR_STOP_REASONS = {"llm_error", "parse_error"}

logger = logging.getLogger(__name__)


_NO_RESPONSE_RETRIEVAL_DECISIONS = {"skipped", "skipped_notes_sufficient"}


def route_after_investigator(state: AgentState) -> str:
    """
    Route after the Investigator runs.

    Skip the Responder for reviews that don't merit a drafted reply.

    LOAD-BEARING INVARIANT:
        Two `retrieval_decision` values route to `skip_response`:

        - `"skipped"` — the deterministic category gate in
          `investigator._should_retrieve(category)` fired (today: `category == "other"`).
          Zero LLM cost path.
        - `"skipped_notes_sufficient"` — the Investigator LLM judged from cluster
          notes that no drafted response is warranted AND made no tool calls. Also
          routes through the early-return path with `stop_reason="no_response_needed"`,
          so Responder/Critic are never invoked and no empty `EvidencePackage` leaks
          downstream.

        Any other `retrieval_decision` ({"retrieved", "insufficient"}) routes to
        the Responder. If a future change adds another "skip but still draft"
        path, it MUST NOT reuse either of these values — add a new one and keep
        this set in sync.
    """
    ep = state.get("evidence_package", {}) or {}
    if ep.get("retrieval_decision") in _NO_RESPONSE_RETRIEVAL_DECISIONS:
        return "skip_response"
    return "respond"

def _create_checkpointer():
    """
    Create a checkpoint backend based on config.

    Returns:
        A checkpointer object (SqliteSaver/MemorySaver)
    """
    if CHECKPOINT_BACKEND == "sqlite":
        import sqlite3
        conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        # Store connection reference for cleanup
        checkpointer._conn = conn  # type: ignore[attr-defined]
        logger.info(f"Using SQLite checkpointer at {CHECKPOINT_DB_PATH}")
        return checkpointer
    else:
        checkpointer = MemorySaver()
        logger.info("Using in-memory checkpointer (state lost on exit).")
        return checkpointer

def build_graph():
    """
    Construct and compile the agent graph.

    Returns:
        A compiled LangGraph application ready to invoke.
    """
    graph = StateGraph(AgentState)

    graph.add_node("coordinator", coordinator_node)
    graph.add_node("investigator", investigator_node)
    graph.add_node("responder", responder_node)
    graph.add_node("critic", critic_node)
    graph.add_node("human_approval", human_approval_node)

    graph.set_entry_point("coordinator")

    graph.add_conditional_edges(
        "coordinator",
        route_from_coordinator,
        {
            "investigate": "investigator",
            "respond": "responder",
            "human_approval": "human_approval",  # action-freeze override
            "done": END,
        },
    )

    graph.add_conditional_edges(
        "investigator",
        route_after_investigator,
        {
            "respond": "responder",
            "skip_response": "coordinator",
        },
    )

    # Responder → critic, unless Responder hit a terminal error (skip critic, go to coordinator for audit-log + END)
    graph.add_conditional_edges(
        "responder",
        lambda state: "coordinator" if state.get("stop_reason") in TERMINAL_ERROR_STOP_REASONS else "critic",
        {
            "critic": "critic",
            "coordinator": "coordinator",
        },
    )

    # Critic approved → human review gate; Critic rejected → coordinator for revision.
    # Terminal critic errors → coordinator (audit-log + END).
    def _route_from_critic(state):
        if state.get("stop_reason") in TERMINAL_ERROR_STOP_REASONS:
            return "coordinator"
        return "human_approval" if state.get("approved", False) else "coordinator"

    graph.add_conditional_edges(
        "critic",
        _route_from_critic,
        {
            "human_approval": "human_approval",
            "coordinator": "coordinator",
        },
    )

    graph.add_conditional_edges(
        "human_approval",
        route_from_human_approval,
        {
            "coordinator": "coordinator",
            "done": END,
        },
    )

    checkpointer = _create_checkpointer()
    app = graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_approval"],
    )

    logger.info("Agent graph compiled successfully.")
    return app