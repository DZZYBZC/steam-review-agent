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
from utils import load_skill
from config import (
    RESPONDER_MODEL,
    RESPONDER_TEMPERATURE,
    RESPONDER_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic()
SYSTEM_PROMPT = load_skill("draft-response")


def _format_evidence_for_responder(evidence: dict) -> str:
    """Format the evidence package into the text format the skill prompt expects."""
    lines = [
        f"<evidence_summary>{evidence.get('summary', 'No evidence available.')}</evidence_summary>",
        f"<evidence_confidence>{evidence.get('confidence', 0.0)}</evidence_confidence>",
        f"<evidence_relevant_ids>{json.dumps(evidence.get('relevant_ids', []))}</evidence_relevant_ids>",
        f"<known_unknowns>{json.dumps(evidence.get('known_unknowns', []))}</known_unknowns>",
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
) -> str:
    """Build the user message for the LLM call."""
    evidence_text = _format_evidence_for_responder(evidence)

    parts = [
        f"<review_tone>{review_tone}</review_tone>",
        f"<review>{review_text}</review>",
        evidence_text,
    ]

    if iteration > 0 and revision_reason:
        parts.append(f"<previous_draft>{previous_draft}</previous_draft>")
        parts.append(f"<revision_feedback>{revision_reason}</revision_feedback>")

    return "\n\n".join(parts)


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
            system=SYSTEM_PROMPT,
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
    review_tone = state.get("review_tone", "neutral")
    evidence = state.get("evidence_package", {})
    iteration = state.get("iteration_count", 0)
    revision_reason = state.get("revision_reason", "")
    previous_draft = state.get("drafted_response", "")

    if iteration > 0 and revision_reason:
        logger.info(f"Responder: revision cycle {iteration}, fixing: {revision_reason[:100]}")
    else:
        logger.info("Responder: drafting initial response")

    user_message = _build_user_message(
        review_text, review_tone, evidence,
        iteration, revision_reason, previous_draft,
    )

    try:
        data, tokens = _call_responder_llm(user_message)
    except Exception as e:
        logger.error(f"Responder: LLM call failed: {e}")
        return {
            "drafted_response": previous_draft or f"[Draft failed: {e}]",
            "proposed_action": "investigate",
            "source_ids_cited": [],
            "iteration_count": iteration + 1,
            "token_usage": {**state.get("token_usage", {}), "responder": accumulate_tokens(state.get("token_usage", {}).get("responder"), {"input": 0, "output": 0})},
            "node_log": [f"responder: failed — {e}"],
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
            f"cited={len(data.get('source_ids_cited', []))} sources"
        ],
    }
