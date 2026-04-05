"""
test_agent.py — One-off end-to-end test of the full agent graph.

Runs a single review through the complete pipeline with real retrieval
(ChromaDB + BM25) and real Anthropic API calls. Expects patch notes to
already be embedded and reviews to be classified in the database.

This is for inspection, not automated testing. Read the output and verify
the agent's reasoning makes sense.

Usage:
    python test_agent.py                    # pick a random negative review
    python test_agent.py --category technical_issues  # filter by category
    python test_agent.py --review-id 12345  # use a specific review
    python test_agent.py --list              # list available reviews
"""

import argparse
import logging

from agent.graph import build_graph
from agent.state import AgentState
from pipeline.storage import get_connection, load_classified_reviews
from pipeline.classify import classify_tone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("chromadb").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

APP_ID = "2246340"


def load_review(category: str | None = None, review_id: str | None = None) -> dict | None:
    """Load a real classified review from the database."""
    conn = get_connection()
    df = load_classified_reviews(conn, APP_ID)
    conn.close()

    if len(df) == 0:
        logger.error("No classified reviews in database. Run the pipeline first: python main.py 2246340")
        return None

    # Filter to negative reviews (more interesting for testing)
    negatives = df[df["voted_up"] == 0]
    if len(negatives) == 0:
        logger.warning("No negative reviews found, using all reviews")
        negatives = df

    if review_id:
        match = negatives[negatives["review_id"] == review_id]
        if len(match) == 0:
            match = df[df["review_id"] == review_id]
        if len(match) == 0:
            logger.error(f"Review ID '{review_id}' not found in database")
            return None
        row = match.iloc[0]
    elif category:
        filtered = negatives[negatives["primary_category"] == category]
        if len(filtered) == 0:
            logger.error(f"No negative reviews with category '{category}'")
            return None
        row = filtered.sample(1).iloc[0]
    else:
        row = negatives.sample(1).iloc[0]

    return {
        "review_id": row["review_id"],
        "review_text": row["review_text"],
        "primary_category": row["primary_category"],
        "voted_up": row["voted_up"],
    }


def list_reviews():
    """Print available reviews grouped by category."""
    conn = get_connection()
    df = load_classified_reviews(conn, APP_ID)
    conn.close()

    if len(df) == 0:
        print("No classified reviews in database.")
        return

    negatives = df[df["voted_up"] == 0]
    print(f"\nClassified reviews for app {APP_ID}: {len(df)} total, {len(negatives)} negative\n")

    for category, group in negatives.groupby("primary_category"):
        print(f"  {category}: {len(group)} reviews")
        for _, row in group.head(3).iterrows():
            preview = row["review_text"][:80].replace("\n", " ")
            print(f"    [{row['review_id']}] {preview}...")
        if len(group) > 3:
            print(f"    ... and {len(group) - 3} more")
        print()


def run(category: str | None = None, review_id: str | None = None):
    # 1. Load a real review
    review = load_review(category=category, review_id=review_id)
    if not review:
        return

    print(f"\n>>> Review ID: {review['review_id']}")
    print(f">>> Category: {review['primary_category']}")
    print(f">>> Text: {review['review_text'][:150]}...")

    # 2. Classify tone via LLM
    logger.info("Classifying review tone...")
    tone = classify_tone(review["review_text"])
    print(f">>> Tone: {tone}")

    # 3. Build graph
    logger.info("Building agent graph...")
    app = build_graph()

    # 4. Build initial state
    test_state: AgentState = {
        "app_id": APP_ID,
        "review_text": review["review_text"],
        "cluster_summary": {
            "category": review["primary_category"],
            "total_reviews": 1,
            "priority_score": 0.0,
        },
        "review_tone": tone,
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

    # 5. Run
    logger.info(f"Running agent on review '{review['review_id']}'...")
    print()

    result = app.invoke(
        test_state,
        config={"configurable": {"thread_id": f"test-{review['review_id']}"}},
    )

    # 6. Diagnostic report
    print("\n" + "=" * 60)
    print("AGENT DIAGNOSTIC REPORT")
    print("=" * 60)

    # -- Input --
    print(f"\nReview ID:       {review['review_id']}")
    print(f"Category:        {review['primary_category']}")
    print(f"Tone:            {tone}")
    print(f"Review:          {review['review_text'][:200]}")

    # -- Termination --
    print(f"\nStop reason:     {result.get('stop_reason', '???')}")
    print(f"Iterations:      {result.get('iteration_count', 0)}")
    print(f"Approved:        {result.get('approved', False)}")

    # -- Evidence --
    evidence = result.get("evidence_package", {})
    print(f"\n--- Evidence Package ---")
    print(f"Retrieval decision: {evidence.get('retrieval_decision', '???')}")
    print(f"Confidence:         {evidence.get('confidence', 0.0)}")
    print(f"Query used:         {evidence.get('query_used', 'N/A')}")
    print(f"Source IDs:         {evidence.get('source_ids', [])}")
    print(f"Relevant IDs:       {evidence.get('relevant_ids', [])}")
    print(f"Known unknowns:     {evidence.get('known_unknowns', [])}")
    print(f"Summary:            {evidence.get('summary', 'none')}")

    # -- Response --
    print(f"\n--- Drafted Response ---")
    print(result.get("drafted_response", "none"))
    print(f"\nProposed action:    {result.get('proposed_action', '???')}")
    print(f"Source IDs cited:   {result.get('source_ids_cited', [])}")

    # -- Critique --
    print(f"\n--- Critique ---")
    print(f"Approved: {result.get('approved', False)}")
    print(f"Critique: {result.get('critique', 'none')}")
    if not result.get("approved", False):
        print(f"Revision reason: {result.get('revision_reason', 'none')}")

    # -- Node log --
    node_log = result.get("node_log", [])
    print(f"\n--- Node Log ({len(node_log)} entries) ---")
    for entry in node_log:
        print(f"  {entry}")

    # -- Token usage --
    token_usage = result.get("token_usage", {})
    print(f"\n--- Token Usage ---")
    total_in = 0
    total_out = 0
    for node_name, counts in token_usage.items():
        inp = counts.get("input", 0)
        out = counts.get("output", 0)
        total_in += inp
        total_out += out
        print(f"  {node_name:15s}  {inp:>6} in  {out:>6} out")
    print(f"  {'TOTAL':15s}  {total_in:>6} in  {total_out:>6} out")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run agent graph on a real review.")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--review-id", type=str, default=None, help="Use a specific review ID")
    parser.add_argument("--list", action="store_true", help="List available reviews")
    args = parser.parse_args()

    if args.list:
        list_reviews()
    else:
        run(category=args.category, review_id=args.review_id)
