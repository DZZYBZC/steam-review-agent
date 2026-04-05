"""
classify.py — Classifies Steam reviews into categories using an LLM.
"""

import json
import logging
import anthropic
from pydantic import BaseModel, Field, field_validator, model_validator
from utils import load_skill
from pipeline.storage import get_unclassified_reviews, save_classification

from config import (
    CLASSIFIER_MODEL,
    CLASSIFIER_TEMPERATURE,
    CLASSIFIER_MAX_TOKENS,
    REVIEW_CATEGORIES,
    CONFIDENCE_THRESHOLD,
    TONE_CLASSIFIER_MODEL,
    TONE_CLASSIFIER_TEMPERATURE,
    TONE_CLASSIFIER_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

# Pydantic for data validation
class ClassificationResult(BaseModel):
    """
    The structured output from the review classifier.
    """

    primary_category: str
    secondary_categories: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=10)

    @field_validator("primary_category")
    @classmethod
    def validate_primary(cls, primary):
        if primary not in REVIEW_CATEGORIES:
            raise ValueError(f"Invalid category: '{primary}'. Must be one of {REVIEW_CATEGORIES}")
        return primary

    @field_validator("secondary_categories")
    @classmethod
    def validate_secondary(cls, secondary):
        for category in secondary:
            if category not in REVIEW_CATEGORIES:
                raise ValueError(f"Invalid secondary category: '{category}. Must be one of {REVIEW_CATEGORIES}'")
            if category == "other":
                raise ValueError("'other' cannot be a secondary category")
        return secondary

    @model_validator(mode="after")
    def check_no_primary_in_secondary(self):
        if self.primary_category in self.secondary_categories:
            self.secondary_categories = [
                category for category in self.secondary_categories
                if category != self.primary_category
            ]
        return self

client = anthropic.Anthropic()
SYSTEM_PROMPT = load_skill("classify-review")

def call_classifier(review_text: str) -> ClassificationResult:
    """
    Send a single review to the LLM and return a validated ClassificationResult.
    """
    system_prompt = SYSTEM_PROMPT

    try:
        response = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=CLASSIFIER_MAX_TOKENS,
            temperature=CLASSIFIER_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": review_text}
            ],
        )
    except anthropic.APIError as e:
        logger.error(f"Anthropic API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Classifier response was cut off (stop_reason='max_tokens'). "
            "Consider increasing CLASSIFIER_MAX_TOKENS in config."
        )

    if response.stop_reason == "refusal":
        logger.warning(
            f"Model refused to classify this review due to safety concerns."
        )
        raise ValueError("Model refused to classify review.")

    logger.debug(f"Classifier API call: {response.usage.input_tokens} input tokens + {response.usage.output_tokens} output tokens")

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
        logger.error(f"Classifier response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    try:
        result = ClassificationResult.model_validate(data)
    except Exception as e:
        logger.error(f"Classifier output failed validation: {e}")
        logger.error(f"Parsed data was: {data}")
        raise

    return result

def classify_review(review_text: str) -> ClassificationResult | None:
    """
    Classify a single review. Returns a ClassificationResult, or None if classification fails for any reason.
    """
    try:
        result = call_classifier(review_text)

        if result.confidence < CONFIDENCE_THRESHOLD:
            logger.info(f"Low confidence ({result.confidence:.1f}) for category '{result.primary_category}' — flagged for human review.")

        return result

    except Exception as e:
        logger.error(f"Classification failed: {e}")
        return None
    
def run_classification(conn, app_id: str, limit: int | None = None) -> dict:
    """
    Classify all unclassified reviews for a given app.

    Parameters:
        conn: Database connection.
        app_id: The Steam game ID.
        limit: Max number of reviews to classify. None = all.
               Use a small number (5-10) when testing a new prompt.

    Returns:
        A summary dict with counts of what happened.
    """

    if limit is not None and limit <= 0:
        logger.info("Classification limit set to 0 — skipping.")
        return {"total": 0, "classified": 0, "failed": 0}

    df = get_unclassified_reviews(conn, app_id)

    if len(df) == 0:
        logger.info("No unclassified reviews found. Nothing to do.")
        return {"total": 0, "classified": 0, "failed": 0}

    if limit:
        logger.info(f"Targeting {limit} successful classifications.")

    classified = 0
    failed = 0
    attempted = 0

    for _, row in df.iterrows():
        if limit and classified >= limit:
            break

        review_id = row["review_id"]
        review_text = row["review_text"]
        attempted += 1

        if attempted % 10 == 0 or attempted == 1:
            logger.info(f"Classification progress: {classified}/{limit or '?'} successful ({attempted} attempted)")

        result = classify_review(review_text)

        if result is None:
            failed += 1
            continue

        save_classification(
            conn=conn,
            review_id=review_id,
            app_id=app_id,
            result=result,
            model_used=CLASSIFIER_MODEL,
        )
        classified += 1

    summary = {
        "total": attempted,
        "classified": classified,
        "failed": failed
    }

    logger.info(
        f"Classification complete: {classified} classified, {failed} failed out of {attempted} attempted."
    )

    return summary


# --- Tone classification ---

VALID_TONES = ["frustrated", "angry", "sarcastic", "constructive", "neutral", "disappointed", "confused", "appreciative"]
TONE_SYSTEM_PROMPT = load_skill("classify-tone")


def classify_tone(review_text: str) -> str:
    """
    Classify the emotional tone of a review.

    Returns one of: frustrated, angry, sarcastic, constructive, neutral.
    Falls back to "neutral" on any failure.
    """
    try:
        response = client.messages.create(
            model=TONE_CLASSIFIER_MODEL,
            max_tokens=TONE_CLASSIFIER_MAX_TOKENS,
            temperature=TONE_CLASSIFIER_TEMPERATURE,
            system=TONE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": review_text}],
        )
    except anthropic.APIError as e:
        logger.error(f"Tone classifier API call failed: {e}")
        return "neutral"

    content_block = response.content[0]
    if not hasattr(content_block, "text"):
        logger.error("Tone classifier returned non-text response")
        return "neutral"
    raw_text = content_block.text.strip()

    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Tone classifier response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        # Try extracting just the first JSON object (model sometimes appends explanation)
        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(raw_text)
        except (json.JSONDecodeError, ValueError):
            return "neutral"

    tone = data.get("tone", "neutral")
    if tone not in VALID_TONES:
        logger.warning(f"Tone classifier returned invalid tone '{tone}', defaulting to neutral")
        return "neutral"

    return tone