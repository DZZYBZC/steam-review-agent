"""
test_graph.py — Quick verification that the LangGraph agent compiles and runs.

This is not a production file — it's a one-off test to verify Milestone 1 setup.
Run it with: python test_graph.py
"""

import logging

from agent.graph import build_graph
from agent.state import AgentState
from config import TEST_APP_ID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def test_graph():
    """Build the graph, run it on a fake review, and inspect the result."""

    # 1. Build and compile the graph
    logger.info("=== Building graph ===")
    app = build_graph()
    logger.info("Graph compiled successfully.")

    # 2. Create a test input state
    test_state: AgentState = {
        "app_id": TEST_APP_ID,
        "review_text": "Game crashes every time I enter the second dungeon. Tried reinstalling, no fix.",
        "cluster_summary": {
            "category": "technical_issues",
            "total_reviews": 25,
            "priority_score": 72.0,
        },
        "review_tone": "frustrated",
        "iteration_count": 0,
        "approved": False,
        "revision_reason": "",
        "stop_reason": "",
        "evidence_package": {},
        "drafted_response": "",
        "proposed_action": "",
        "source_ids_cited": [],
        "critique": "",
        "node_log": [],
        "token_usage": {},
    }

    # 3. Invoke the graph
    logger.info("=== Running graph on test review ===")
    result = app.invoke(
        test_state,
        config={"configurable": {"thread_id": "test-001"}}
    )

    # 4. Print the results
    print("\n" + "=" * 50)
    print("GRAPH RUN RESULT")
    print("=" * 50)
    print(f"\nStop reason: {result.get('stop_reason', 'unknown')}")
    print(f"Approved: {result.get('approved', False)}")
    print(f"Iterations: {result.get('iteration_count', 0)}")
    print(f"Drafted response: {result.get('drafted_response', 'none')}")
    print(f"Proposed action: {result.get('proposed_action', 'none')}")
    print(f"Critique: {result.get('critique', 'none')}")

    print(f"\nNode log ({len(result.get('node_log', []))} entries):")
    for entry in result.get("node_log", []):
        print(f"  - {entry}")

    # 5. Basic assertions
    assert result.get("stop_reason", "") != "", f"Expected non-empty stop_reason, got '{result.get('stop_reason', '')}'"
    assert result.get("iteration_count", 0) > 0, f"Expected iteration_count > 0, got {result.get('iteration_count', 0)}"
    assert result.get("drafted_response", "") != "", "Expected non-empty drafted_response"
    assert len(result.get("node_log", [])) > 0, "Expected non-empty node_log"

    print("\n✓ All assertions passed. Graph is working correctly.")
    print("=" * 50)


if __name__ == "__main__":
    test_graph()