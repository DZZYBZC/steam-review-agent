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


def route_after_investigator(state: AgentState) -> str:
    """
    Route after the Investigator runs.

    Skip the Responder for reviews that don't merit a drafted reply.

    LOAD-BEARING INVARIANT:
        `evidence_package.retrieval_decision == "skipped"` is currently the
        "no-response-eligible" bucket. The only path that produces this value
        is the deterministic gate in investigator._should_retrieve(category),
        which today fires only for `category == "other"`. Every other path out
        of the Investigator produces retrieval_decision in {"retrieved",
        "insufficient"}.

        If a future change ever makes the Investigator skip retrieval for a
        substantive category (e.g. budget-driven skips on cost-sensitive
        tiers, or a new "subjective_opinion" category we still want to draft
        replies for), this routing rule needs revisiting. Split the skip into
        "skipped — no response eligible" vs "skipped — still draft" by adding
        a sentinel field on EvidencePackage or a new retrieval_decision value
        (e.g. "skipped_eligible" vs "skipped_no_response") and route on that.
    """
    ep = state.get("evidence_package", {}) or {}
    if ep.get("retrieval_decision") == "skipped":
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