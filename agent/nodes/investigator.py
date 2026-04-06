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
from pipeline.classify import classify_tone
from pipeline.storage import (
    get_connection, load_cluster_notes,
    save_cluster_note, find_recent_similar_note,
)
from utils import load_skill, parse_llm_json
from config import (
    CLAUDE_API_KEY,
    INVESTIGATOR_MODEL,
    INVESTIGATOR_TEMPERATURE,
    INVESTIGATOR_MAX_TOKENS,
    RETRIEVAL_CATEGORIES,
    SELF_RAG_MAX_RETRIES,
    CLUSTER_NOTE_AUTO_MIN_SOURCES,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
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
            f"section={meta.get('section', 'unknown')}"
        )
        lines.append(f'    "{r["text"]}"')
    return "\n".join(lines)


def _call_investigator_llm(
    review_text: str, evidence_text: str, category: str,
    cluster_notes_text: str = "",
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

    if cluster_notes_text:
        user_message += f"\n\n{cluster_notes_text}"

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

    if not response.content:
        raise ValueError("Investigator LLM returned empty response")
    content_block = response.content[0]
    if not hasattr(content_block, "text"):
        raise ValueError(f"Expected a text response, got {type(content_block).__name__}")
    raw_text = content_block.text.strip()  # type: ignore[union-attr]

    try:
        data = parse_llm_json(raw_text)
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

    # Tone classification (before retrieval — independent of evidence)
    if category == "other":
        review_tone = "skipped"
        logger.info("Investigator: skipped tone classification for category 'other'")
    else:
        review_tone = classify_tone(review_text)
        if review_tone == "unknown":
            logger.warning("Investigator: tone classification failed (API error fallback)")
        else:
            logger.info(f"Investigator: classified tone as '{review_tone}'")

    total_tokens = {"input": 0, "output": 0}

    # Load cluster notes for additional context
    cluster_notes_text = ""
    notes_count = 0
    if app_id and category:
        try:
            conn = get_connection()
            try:
                notes = load_cluster_notes(conn, app_id, category)
                notes_count = len(notes)
            finally:
                conn.close()
            if notes:
                lines = ["<cluster_notes>"]
                for n in notes:
                    date = n.get("created_at", "")[:10]
                    lines.append(f"[{n['note_type']}] ({date}): {n['note_text']}")
                lines.append("</cluster_notes>")
                cluster_notes_text = "\n".join(lines)
        except Exception as e:
            logger.warning(f"Investigator: failed to load cluster notes: {e}")

    notes_log = f"investigator: loaded {notes_count} cluster notes"

    # Stage 1: Deterministic gate
    if not _should_retrieve(category):
        logger.info(f"Investigator: skipping retrieval for category '{category}'")
        evidence = EvidencePackage(
            retrieval_decision="skipped",
            retrieval_reasoning=f"Category '{category}' is not in RETRIEVAL_CATEGORIES — no patch note evidence expected.",
        )
        return {
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: tone={review_tone}, skipped retrieval — category '{category}'", notes_log],
        }

    # On a revision re-entry triggered by an evidence-type critic rejection,
    # the Critic provides a retrieval_hint to seed the next search. If the hint
    # is missing (Critic forgot, or reason_type was "drafting"), fall back to
    # the default query construction. Bad hints are handled inside the self-RAG
    # retry loop below (the sufficiency check will fail and reformulated_query
    # will take over within the same invocation).
    retrieval_hint = state.get("retrieval_hint", "") or ""

    try:
        # Stage 2: Hybrid retrieval
        if retrieval_hint:
            query = retrieval_hint
            logger.info(f"Investigator: using critic retrieval_hint as query: {retrieval_hint!r}")
        else:
            query = review_text
        results = retrieve(query, app_id)

        logger.info(f"Investigator: retrieved {len(results)} chunks for initial query")

        # Stage 3: Self-RAG assessment + retry loop
        retries = 0
        while True:
            evidence_text = _format_evidence_for_llm(results)
            assessment, tokens = _call_investigator_llm(
                review_text, evidence_text, category, cluster_notes_text
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
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: tone={review_tone}, failed — {e}", notes_log],
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

    # Auto-save known_issue note when the investigator commits to sufficiency
    # with enough relevant sources. Deterministic gate — the LLM-self-reported
    # confidence float is not used here (see F3 in the design review).
    if (
        retrieval_decision == "retrieved"
        and is_sufficient
        and len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES
    ):
        try:
            conn = get_connection()
            try:
                existing = find_recent_similar_note(
                    conn, app_id, category, "known_issue",
                    source_review_id=state.get("review_id", ""),
                )
                if not existing:
                    save_cluster_note(
                        conn, app_id=app_id, category=category,
                        note_type="known_issue",
                        note_text=evidence.summary,
                        source_review_id=state.get("review_id", ""),
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Investigator: failed to auto-save known_issue note: {e}")

    return {
        "review_tone": review_tone,
        "evidence_package": evidence.to_dict(),
        "retrieval_hint": "",  # consumed — do not reuse across cycles
        "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
        "node_log": [
            f"investigator: tone={review_tone}, {retrieval_decision} — confidence={evidence.confidence:.2f}, "
            f"sources={len(sources)}, retries={retries}",
            notes_log,
        ],
    }
