"""
agent/nodes/critic.py — Reviews the drafted response for quality.

Evaluates the Responder's draft against a 6-point checklist:
hallucination, overconfidence, known unknowns, tone, completeness, action.
Approves or rejects with a specific, actionable revision reason.
"""

import json
import logging
import anthropic

from agent.state import AgentState
from agent.utils import accumulate_tokens, format_evidence_sources
from utils import load_skill, parse_llm_json
from config import (
    CLAUDE_API_KEY,
    CRITIC_MODEL,
    CRITIC_TEMPERATURE,
    CRITIC_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
SYSTEM_PROMPT = load_skill("critique-draft")


def _build_user_message(
    review_text: str,
    review_tone: str,
    evidence: dict,
    drafted_response: str,
    proposed_action: str,
    source_ids_cited: list[str],
) -> str:
    """Build the user message for the Critic LLM call."""
    parts = [
        f"<review_tone>{review_tone}</review_tone>",
        f"<review>{review_text}</review>",
        f"<evidence_summary>{evidence.get('summary', 'No evidence available.')}</evidence_summary>",
        f"<evidence_confidence>{evidence.get('confidence', 0.0)}</evidence_confidence>",
        f"<evidence_relevant_ids>{json.dumps(evidence.get('relevant_ids', []))}</evidence_relevant_ids>",
        f"<known_unknowns>{json.dumps(evidence.get('known_unknowns', []))}</known_unknowns>",
        format_evidence_sources(evidence),
        f"<draft_response>{drafted_response}</draft_response>",
        f"<draft_action>{proposed_action}</draft_action>",
        f"<draft_source_ids_cited>{json.dumps(source_ids_cited)}</draft_source_ids_cited>",
    ]

    return "\n\n".join(parts)


def _call_critic_llm(user_message: str) -> tuple[dict, dict[str, int]]:
    """
    Call the LLM to evaluate the drafted response.

    Returns:
        A tuple of (parsed JSON dict, token counts dict).
    """
    try:
        response = client.messages.create(
            model=CRITIC_MODEL,
            max_tokens=CRITIC_MAX_TOKENS,
            temperature=CRITIC_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Critic API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Critic response was cut off (stop_reason='max_tokens'). "
            "Consider increasing CRITIC_MAX_TOKENS in config."
        )

    if response.stop_reason == "refusal":
        logger.warning("Model refused to critique draft due to safety concerns.")
        raise ValueError("Model refused to critique draft.")

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    logger.debug(
        f"Critic API call: {tokens['input']} input + {tokens['output']} output tokens"
    )

    if not response.content:
        raise ValueError("Critic LLM returned empty response")
    content_block = response.content[0]
    if not hasattr(content_block, "text"):
        raise ValueError(f"Expected a text response, got {type(content_block).__name__}")
    raw_text = content_block.text.strip()  # type: ignore[union-attr]

    try:
        data = parse_llm_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Critic response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    return data, tokens


def critic_node(state: AgentState) -> dict:
    """
    Evaluate the drafted response against the evidence package.

    Reads: review_text, review_tone, evidence_package, drafted_response, proposed_action.
    Does not read: app_id, token_usage, iteration_count.

    Returns approved/rejected with critique and revision_reason.
    """
    review_text = state.get("review_text", "")
    review_tone = state.get("review_tone", "neutral")
    evidence = state.get("evidence_package", {})
    drafted_response = state.get("drafted_response", "")
    proposed_action = state.get("proposed_action", "monitor")

    logger.info(f"Critic: evaluating draft ({len(drafted_response)} chars)")

    source_ids_cited = state.get("source_ids_cited", [])

    user_message = _build_user_message(
        review_text, review_tone, evidence,
        drafted_response, proposed_action, source_ids_cited,
    )

    try:
        data, tokens = _call_critic_llm(user_message)
    except Exception as e:
        logger.error(f"Critic: LLM call failed: {e}")
        return {
            "critique": f"Critic failed: {e}",
            "approved": False,
            "revision_reason": "Critic evaluation failed — auto-rejecting for manual review.",
            "token_usage": state.get("token_usage", {}),
            "node_log": [f"critic: failed — {e}"],
        }

    approved = data.get("approved", False)
    critique = data.get("critique", "")
    revision_reason = data.get("revision_reason", "")

    if approved:
        logger.info("Critic: draft approved")
    else:
        logger.info(f"Critic: draft rejected — {revision_reason[:100]}")

    return {
        "critique": critique,
        "approved": approved,
        "revision_reason": revision_reason,
        "stop_reason": "approved" if approved else "",
        "token_usage": {**state.get("token_usage", {}), "critic": accumulate_tokens(state.get("token_usage", {}).get("critic"), tokens)},
        "node_log": [
            f"critic: {'approved' if approved else 'rejected'}"
            + (f" — {revision_reason[:80]}" if not approved else "")
        ],
    }
