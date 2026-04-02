"""
agent/nodes/investigator.py — Gathers evidence for a review response.

Milestone 1: Placeholder that passes through with minimal state updates.
Milestone 3: Will use adaptive retrieval, hybrid search, and structured
             evidence packaging.
"""

import logging

from agent.state import AgentState

logger = logging.getLogger(__name__)


def investigator_node(state: AgentState) -> dict:
    """
    Gather evidence relevant to the review and its cluster.

    Milestone 1 (placeholder):
        Returns a stub evidence package so downstream nodes can run.

    Milestone 3 (real logic):
        - Checks structured cluster notes for prior knowledge
        - Decides whether deeper retrieval is needed (adaptive retrieval)
        - Runs hybrid search (vector + BM25) if needed
        - Reranks results
        - Builds a structured EvidencePackage with source_ids and confidence
    """
    review = state.get("review_text", "")
    logger.info(f"Investigator: processing review ({len(review)} chars)")

    # Placeholder evidence package — will become a Pydantic EvidencePackage in M3
    evidence = {
        "summary": "Placeholder evidence — no retrieval implemented yet.",
        "confidence": 0.0,
        "source_ids": [],
        "known_unknowns": ["Retrieval not yet implemented"],
        "retrieval_decision_reason": "Milestone 1 placeholder — skipping retrieval",
    }

    return {
        "evidence_package": evidence,
        "node_log": ["investigator: placeholder — returned stub evidence"],
    }