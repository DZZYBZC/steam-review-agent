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
from config import CHECKPOINT_BACKEND, CHECKPOINT_DB_PATH

logger = logging.getLogger(__name__)

def _create_checkpointer():
    """
    Create a checkpoint backend based on config.

    Returns:
        A checkpointer object (SqliteSaver/MemorySaver)
    """
    if CHECKPOINT_BACKEND == "sqlite":
        import sqlite3
        conn = sqlite3.connect(CHECKPOINT_DB_PATH)
        checkpointer = SqliteSaver(conn)
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

    graph.add_edge("investigator", "responder")
    graph.add_edge("responder", "critic")
    graph.add_edge("critic", "coordinator")

    checkpointer = _create_checkpointer()
    app = graph.compile(checkpointer=checkpointer)

    logger.info("Agent graph compiled successfully.")
    return app