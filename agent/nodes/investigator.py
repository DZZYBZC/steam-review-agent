"""
agent/nodes/investigator.py — Gathers evidence for a review response.

Three stages:
1. Deterministic gate: skip retrieval for categories unlikely to have patch note evidence.
2. Hybrid retrieval: vector + BM25 → RRF → cross-encoder rerank.
3. Self-RAG: LLM assesses relevance and sufficiency, optionally retries with a reformulated query.
"""

import json
import logging
import anthropic

from agent.state import AgentState
from agent.models import EvidencePackage
from pipeline.retrieve import retrieve
from utils import load_skill
from config import (
    INVESTIGATOR_MODEL,
    INVESTIGATOR_TEMPERATURE,
    INVESTIGATOR_MAX_TOKENS,
    RETRIEVAL_CATEGORIES,
    SELF_RAG_MAX_RETRIES,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic()
SYSTEM_PROMPT = load_skill("investigate-evidence")


def _should_retrieve(category: str) -> bool:
    """Deterministic gate: only retrieve for categories likely to have patch note evidence."""
    return category in RETRIEVAL_CATEGORIES


def _format_evidence_for_llm(results: list[dict]) -> str:
    """Format retrieved chunks into the text format the skill prompt expects."""
    if not results:
        return "(no chunks retrieved — retrieval returned empty results)"

    lines = []
    for i, r in enumerate(results):
        meta = r["metadata"]
        lines.append(
            f"[{i}] chunk_id={r['chunk_id']} "
            f"version={meta.get('patch_version', 'unknown')} "
            f"section={meta.get('section', 'unknown')} "
            f"relevance={r.get('relevance_score', 0.0):.2f}"
        )
        lines.append(f'    "{r["text"]}"')
    return "\n".join(lines)


def _call_investigator_llm(
    review_text: str, evidence_text: str, category: str
) -> tuple[dict, dict[str, int]]:
    """
    Call the LLM to assess evidence relevance and sufficiency.

    Returns:
        A tuple of (parsed JSON dict, token counts dict).
    """
    user_message = (
        f"<category>{category}</category>\n\n"
        f"<complaint>{review_text}</complaint>\n\n"
        f"<evidence>\n{evidence_text}\n</evidence>"
    )

    try:
        response = client.messages.create(
            model=INVESTIGATOR_MODEL,
            max_tokens=INVESTIGATOR_MAX_TOKENS,
            temperature=INVESTIGATOR_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Investigator API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Investigator response was cut off (stop_reason='max_tokens'). "
            "Consider increasing INVESTIGATOR_MAX_TOKENS in config."
        )

    if response.stop_reason == "refusal":
        logger.warning("Model refused to assess evidence due to safety concerns.")
        raise ValueError("Model refused to assess evidence.")

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    logger.debug(
        f"Investigator API call: {tokens['input']} input + {tokens['output']} output tokens"
    )

    content_block = response.content[0]
    if not hasattr(content_block, "text"):
        raise ValueError(f"Expected a text response, got {type(content_block).__name__}")
    raw_text = content_block.text.strip()  # type: ignore[union-attr]

    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Investigator response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    return data, tokens


def investigator_node(state: AgentState) -> dict:
    """
    Gather evidence relevant to the review and its cluster.

    1. Checks review category against RETRIEVAL_CATEGORIES — skips if "other".
    2. Runs hybrid retrieval pipeline (vector + BM25 → RRF → rerank).
    3. Calls LLM to assess relevance, confidence, and sufficiency.
    4. If insufficient, retries with a reformulated query (up to SELF_RAG_MAX_RETRIES).
    """
    review_text = state.get("review_text", "")
    app_id = state.get("app_id", "")
    cluster = state.get("cluster_summary", {})
    category = cluster.get("category", "other")

    logger.info(f"Investigator: processing review ({len(review_text)} chars), category={category}")

    total_tokens = {"input": 0, "output": 0}

    # Stage 1: Deterministic gate
    if not _should_retrieve(category):
        logger.info(f"Investigator: skipping retrieval for category '{category}'")
        evidence = EvidencePackage(
            retrieval_decision="skipped",
            retrieval_reasoning=f"Category '{category}' is not in RETRIEVAL_CATEGORIES — no patch note evidence expected.",
        )
        return {
            "evidence_package": evidence.to_dict(),
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: skipped retrieval — category '{category}'"],
        }

    try:
        # Stage 2: Hybrid retrieval
        query = review_text
        results = retrieve(query, app_id)

        logger.info(f"Investigator: retrieved {len(results)} chunks for initial query")

        # Stage 3: Self-RAG assessment + retry loop
        retries = 0
        while True:
            evidence_text = _format_evidence_for_llm(results)
            assessment, tokens = _call_investigator_llm(
                review_text, evidence_text, category
            )
            total_tokens["input"] += tokens["input"]
            total_tokens["output"] += tokens["output"]

            is_sufficient = assessment.get("is_sufficient", True)

            if is_sufficient or retries >= SELF_RAG_MAX_RETRIES:
                if not is_sufficient:
                    logger.info(
                        f"Investigator: evidence insufficient but max retries ({SELF_RAG_MAX_RETRIES}) reached"
                    )
                break

            # Retry with reformulated query
            reformulated = assessment.get("reformulated_query", "")
            if not reformulated:
                logger.info("Investigator: insufficient but no reformulated query provided — stopping")
                break

            retries += 1
            logger.info(f"Investigator: retry {retries} with reformulated query: '{reformulated}'")
            query = reformulated
            results = retrieve(query, app_id)
            logger.info(f"Investigator: retrieved {len(results)} chunks for reformulated query")

    except Exception as e:
        logger.error(f"Investigator: retrieval/assessment failed: {e}")
        evidence = EvidencePackage(
            retrieval_decision="insufficient",
            retrieval_reasoning=f"Retrieval or assessment failed: {e}",
        )
        return {
            "evidence_package": evidence.to_dict(),
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: failed — {e}"],
        }

    # Build evidence package
    relevant_ids = assessment.get("relevant_ids", [])
    retrieval_decision = "retrieved" if relevant_ids and is_sufficient else "insufficient"
    relevant_set = set(relevant_ids)
    sources = [r for r in results if r["chunk_id"] in relevant_set]

    if is_sufficient:
        reasoning = f"Retrieved {len(sources)} relevant chunks with confidence {assessment.get('confidence', 0.0):.2f}"
    else:
        reasoning = f"Evidence insufficient after {retries + 1} retrieval attempts"

    evidence = EvidencePackage(
        summary=assessment.get("summary", ""),
        confidence=assessment.get("confidence", 0.0),
        relevant_ids=relevant_ids,
        source_ids=[r["chunk_id"] for r in results],
        sources=sources,
        known_unknowns=assessment.get("known_unknowns", []),
        retrieval_decision=retrieval_decision,
        retrieval_reasoning=reasoning,
        query_used=query,
    )

    return {
        "evidence_package": evidence.to_dict(),
        "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
        "node_log": [
            f"investigator: {retrieval_decision} — confidence={evidence.confidence:.2f}, "
            f"sources={len(sources)}, retries={retries}"
        ],
    }
