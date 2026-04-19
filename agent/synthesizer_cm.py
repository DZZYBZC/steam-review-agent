"""
agent/synthesizer_cm.py — CM batch synthesizer.

Takes a CM goal, the plan from CMPlanner, and the list of per-candidate agent
results, and returns a markdown rollup with three fixed sections (Pattern,
Recommended escalations, Per-review drafts). API shape mirrors the other
Haiku-backed skills in this project (see critic.py).
"""

import json
import logging

import anthropic

from config import (
    CLAUDE_API_KEY,
    CLASSIFIER_MODEL,
)
from utils import escape_xml, load_skill

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_skill("synthesize-cm-batch")

SYNTH_MODEL = CLASSIFIER_MODEL
SYNTH_TEMPERATURE = 0.3
SYNTH_MAX_TOKENS = 2048


class CMSynthesizer:
    """Single-call Haiku synthesizer that produces a markdown rollup for a CM batch."""

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)

    def synthesize(self, goal: str, plan: dict, candidates: list[dict]) -> str:
        """Produce a markdown batch summary.

        Args:
            goal: The CM's original natural-language request.
            plan: The structured plan dict from CMPlanner.plan().
            candidates: Per-review result dicts (see skills/synthesize-cm-batch/SKILL.md
                for the expected shape).

        Returns:
            Raw markdown string (trimmed of surrounding whitespace).

        Raises:
            ValueError: on refusal or empty response.
            anthropic.APIError: on API-layer failure.
        """
        user_message = _build_user_message(goal, plan, candidates)

        response = self.client.messages.create(
            model=SYNTH_MODEL,
            max_tokens=SYNTH_MAX_TOKENS,
            temperature=SYNTH_TEMPERATURE,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )

        if response.stop_reason == "refusal":
            raise ValueError("CM synthesizer model refused the request.")
        if response.stop_reason == "max_tokens":
            logger.warning("CM synthesizer response cut off at max_tokens.")

        if not response.content:
            raise ValueError("CM synthesizer returned empty response.")
        block = response.content[0]
        if not hasattr(block, "text"):
            raise ValueError(f"CM synthesizer returned non-text content: {type(block).__name__}")
        return block.text.strip()  # type: ignore[union-attr]


def _build_user_message(goal: str, plan: dict, candidates: list[dict]) -> str:
    return "\n\n".join([
        f"<goal>{escape_xml(goal)}</goal>",
        f"<plan>{escape_xml(json.dumps(plan, ensure_ascii=False))}</plan>",
        f"<candidates>{escape_xml(json.dumps(candidates, ensure_ascii=False))}</candidates>",
    ])
