"""
agent/nodes/responder.py — Drafts a player-facing response.

Reads the evidence package from the Investigator, matches tone to the review,
and produces a grounded response with an internal action recommendation.
On revision cycles, incorporates Critic feedback and revises the previous draft.
"""

import json
import logging
import anthropic

from agent.state import AgentState
from agent.utils import accumulate_tokens, format_evidence_sources
from pipeline.storage import get_connection, load_feedback_examples
from utils import load_skill, parse_llm_json, escape_xml
from config import (
    CLAUDE_API_KEY,
    RESPONDER_MODEL,
    RESPONDER_TEMPERATURE,
    RESPONDER_MAX_TOKENS,
    RESPONDER_USE_FEEDBACK_EXAMPLES,
    RESPONDER_FEEDBACK_EXAMPLES,
    RESPONDER_FEEDBACK_MAX_REVIEW_CHARS,
    RESPONDER_FEEDBACK_MAX_RESPONSE_CHARS,
    RESPONDER_FEEDBACK_MAX_SUMMARY_CHARS,
    RESPONDER_FEEDBACK_SELECTION_POOL,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)
SYSTEM_PROMPT = load_skill("draft-response")


def _format_evidence_for_responder(evidence: dict) -> str:
    """Format the evidence package into the text format the skill prompt expects."""
    lines = [
        f"<evidence_summary>{escape_xml(evidence.get('summary', 'No evidence available.'))}</evidence_summary>",
        f"<evidence_confidence>{evidence.get('confidence', 0.0)}</evidence_confidence>",
        f"<evidence_relevant_ids>{escape_xml(json.dumps(evidence.get('relevant_ids', [])))}</evidence_relevant_ids>",
        f"<known_unknowns>{escape_xml(json.dumps(evidence.get('known_unknowns', [])))}</known_unknowns>",
        format_evidence_sources(evidence),
    ]
    return "\n".join(lines)


def _build_user_message(
    review_text: str,
    review_tone: str,
    evidence: dict,
    iteration: int,
    revision_reason: str,
    previous_draft: str,
    feedback_examples_block: str = "",
) -> str:
    """Build the user message for the LLM call."""
    evidence_text = _format_evidence_for_responder(evidence)

    parts = [
        f"<review_tone>{escape_xml(review_tone)}</review_tone>",
        f"<review>{escape_xml(review_text)}</review>",
        evidence_text,
    ]

    if iteration > 0 and revision_reason:
        parts.append(f"<previous_draft>{escape_xml(previous_draft)}</previous_draft>")
        parts.append(f"<revision_feedback>{escape_xml(revision_reason)}</revision_feedback>")

    if feedback_examples_block:
        parts.append(feedback_examples_block)

    return "\n\n".join(parts)


def _format_feedback_examples(
    examples: list[dict],
    max_review_chars: int,
    max_response_chars: int,
    max_summary_chars: int,
) -> str:
    """Format approved audit_log examples as XML for the user message."""
    if not examples:
        return ""
    parts = ["<feedback_examples>"]
    for i, ex in enumerate(examples, 1):
        parts.append(f'<approved_example index="{i}">')

        review_tone = ex.get("review_tone") or ""
        if review_tone:
            parts.append(f"<review_tone>{escape_xml(review_tone)}</review_tone>")

        review_text = (ex.get("review_text") or "")[:max_review_chars]
        if review_text:
            parts.append(f"<review>{escape_xml(review_text)}</review>")

        evidence_summary = (ex.get("evidence_summary") or "")[:max_summary_chars]
        if evidence_summary:
            parts.append(f"<evidence_summary>{escape_xml(evidence_summary)}</evidence_summary>")

        confidence = ex.get("evidence_confidence")
        if confidence is not None:
            parts.append(f"<evidence_confidence>{confidence}</evidence_confidence>")

        action = ex.get("proposed_action") or ""
        if action:
            parts.append(f"<proposed_action>{escape_xml(action)}</proposed_action>")

        response = (ex.get("drafted_response") or "")[:max_response_chars]
        if response:
            parts.append(f"<response>{escape_xml(response)}</response>")

        parts.append("</approved_example>")
    parts.append("</feedback_examples>")
    return "\n".join(parts)


def _call_responder_llm(user_message: str) -> tuple[dict, dict[str, int]]:
    """
    Call the LLM to draft a player-facing response.

    Returns:
        A tuple of (parsed JSON dict, token counts dict).
    """
    try:
        response = client.messages.create(
            model=RESPONDER_MODEL,
            max_tokens=RESPONDER_MAX_TOKENS,
            temperature=RESPONDER_TEMPERATURE,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Responder API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Responder response was cut off (stop_reason='max_tokens'). "
            "Consider increasing RESPONDER_MAX_TOKENS in config."
        )

    if response.stop_reason == "refusal":
        logger.warning("Model refused to draft response due to safety concerns.")
        raise ValueError("Model refused to draft response.")

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    logger.debug(
        f"Responder API call: {tokens['input']} input + {tokens['output']} output tokens"
    )

    if not response.content:
        raise ValueError("Responder LLM returned empty response")
    content_block = response.content[0]
    if not hasattr(content_block, "text"):
        raise ValueError(f"Expected a text response, got {type(content_block).__name__}")
    raw_text = content_block.text.strip()  # type: ignore[union-attr]

    try:
        data = parse_llm_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Responder response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    return data, tokens


def responder_node(state: AgentState) -> dict:
    """
    Draft a player-facing response and propose an internal action.

    First pass: reads review + evidence + tone.
    Revision cycles: also reads revision_reason and previous draft.
    """
    review_text = state.get("review_text", "")
    review_tone = state.get("review_tone", "")
    evidence = state.get("evidence_package", {})
    iteration = state.get("iteration_count", 0)
    revision_reason = state.get("revision_reason", "")
    previous_draft = state.get("drafted_response", "")

    if iteration > 0 and revision_reason:
        logger.info(f"Responder: revision cycle {iteration}, fixing: {revision_reason[:100]}")
    else:
        logger.info("Responder: drafting initial response")

    app_id = state.get("app_id", "")
    category = state.get("cluster_summary", {}).get("category", "")

    feedback_examples_block = ""
    feedback_log = "responder: feedback examples disabled"
    if RESPONDER_USE_FEEDBACK_EXAMPLES and iteration == 0 and app_id and category:
        try:
            conn = get_connection()
            try:
                feedback_examples, selection_log = load_feedback_examples(
                    conn, app_id, category,
                    n=RESPONDER_FEEDBACK_EXAMPLES,
                    pool_size=RESPONDER_FEEDBACK_SELECTION_POOL,
                )
            finally:
                conn.close()
            selection_summary = ", ".join(
                f"{s['reason']}({s['action']}/{s['confidence']})"
                for s in selection_log
            )
            feedback_log = (
                f"responder: selected {len(feedback_examples)} feedback examples "
                f"[{selection_summary}]"
            )
            feedback_examples_block = _format_feedback_examples(
                feedback_examples,
                RESPONDER_FEEDBACK_MAX_REVIEW_CHARS,
                RESPONDER_FEEDBACK_MAX_RESPONSE_CHARS,
                RESPONDER_FEEDBACK_MAX_SUMMARY_CHARS,
            )
        except Exception as e:
            logger.warning(f"Responder: failed to load feedback examples: {e}")
            feedback_log = f"responder: feedback examples failed: {e}"

    user_message = _build_user_message(
        review_text, review_tone, evidence,
        iteration, revision_reason, previous_draft,
        feedback_examples_block=feedback_examples_block,
    )

    try:
        data, tokens = _call_responder_llm(user_message)
    except json.JSONDecodeError as e:
        logger.error(f"Responder: LLM response parse failed: {e}")
        return {
            "drafted_response": previous_draft or f"[Parse failed: {e}]",
            "stop_reason": "parse_error",
            "node_log": [f"responder: parse_error — {e}", feedback_log],
        }
    except Exception as e:
        logger.error(f"Responder: LLM call failed: {e}")
        return {
            "drafted_response": previous_draft or f"[Draft failed: {e}]",
            "stop_reason": "llm_error",
            "node_log": [f"responder: llm_error — {e}", feedback_log],
        }

    return {
        "drafted_response": data.get("response_text", ""),
        "proposed_action": data.get("proposed_action", "monitor"),
        "source_ids_cited": data.get("source_ids_cited", []),
        "iteration_count": iteration + 1,
        "token_usage": {**state.get("token_usage", {}), "responder": accumulate_tokens(state.get("token_usage", {}).get("responder"), tokens)},
        "node_log": [
            f"responder: drafted (iteration {iteration + 1}), "
            f"action={data.get('proposed_action', 'monitor')}, "
            f"cited={len(data.get('source_ids_cited', []))} sources",
            feedback_log,
        ],
    }
