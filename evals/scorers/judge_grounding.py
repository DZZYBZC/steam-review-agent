"""
evals/scorers/judge_grounding.py — V1.5 LLM judge for the `low_conf_with_cite`
diagnostic flag.

The deterministic scorer (`grounding_band_compliance`) cannot tell honest
hedging from misleading fix claims when the responder cites patches at
confidence < 0.4. It just emits a `flag="low_conf_with_cite"`. This module
asks Claude to make that ruling, one case at a time.

Mirrors `evals/scorers/gating_accuracy.py`'s sibling-scorer shape:
  - per-case function: judge_grounding(case, record, scored_per_case)
  - batch function:    judge_grounding_batch(cases, records, scored)

The judge runs only against cases the deterministic scorer flagged. Cases
without the flag return None and are skipped.

Caching: results are cached on disk under `evals/judge_cache/`. The cache key
covers every input that affects the ruling — see _cache_key() for the full
list. Re-running this module against the same run file with the same skill,
model, and input formatting makes ZERO LLM calls.

If anything that affects the rendered judge input changes (input format code,
chunk filter, output schema), bump JUDGE_INPUT_VERSION below.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

from config import (
    CLAUDE_API_KEY,
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL,
    JUDGE_TEMPERATURE,
)
from utils import load_skill, parse_llm_json

logger = logging.getLogger(__name__)

# Bump this when anything OTHER than the skill file changes the rendered judge
# input or the output contract — e.g., _build_judge_user_message() shape, the
# cited-chunk filter, the JSON output schema. The cache key includes this
# constant so a bump invalidates every cached entry.
#
#   v1: initial release. Inputs: review, draft, confidence, cited chunks
#       (id + text). Output: {"ruling", "rationale"}.
JUDGE_INPUT_VERSION = 1

JUDGE_CACHE_DIR = Path(__file__).resolve().parents[1] / "judge_cache"
SKILL_NAME = "judge-grounding"
SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME / "SKILL.md"

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)
SYSTEM_PROMPT = load_skill(SKILL_NAME)


# Local trust-boundary model — judge never enters the agent path.

class JudgeRuling(BaseModel):
    ruling: str = Field(..., pattern="^(honest_hedge|misleading_fix_claim|unclear)$")
    rationale: str = Field(default="")


# ---------- Cache key + I/O ----------------------------------------------

def _sha8(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:8]


def _skill_sha() -> str:
    """First 8 hex chars of sha256 of the skill file's raw bytes."""
    return _sha8(SKILL_PATH.read_bytes())


def _cache_key(
    run_file_basename: str,
    case_id: str,
    user_message: str,
) -> str:
    """
    Build the cache filename. Every component that affects the ruling is in
    the key, so any change invalidates the cached entry:

      1. run_file_basename — different runs have different drafts
      2. case_id           — obvious
      3. JUDGE_MODEL       — model swap is a behavior change
      4. skill_sha         — prompt text change
      5. JUDGE_INPUT_VERSION — manual bump for input/output shape changes
      6. user_message_sha  — belt-and-suspenders: catches changes the dev
                              forgot to bump JUDGE_INPUT_VERSION for
    """
    skill_sha = _skill_sha()
    user_msg_sha = _sha8(user_message.encode("utf-8"))
    model_safe = JUDGE_MODEL.replace("/", "_")
    return (
        f"{run_file_basename}__{case_id}__{model_safe}"
        f"__{skill_sha}__v{JUDGE_INPUT_VERSION}__{user_msg_sha}.json"
    )


def _cache_read(cache_path: Path) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        with cache_path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Judge cache read failed for {cache_path.name}: {e}")
        return None


def _cache_write(cache_path: Path, payload: dict) -> None:
    JUDGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w") as f:
        json.dump(payload, f, indent=2)


# ---------- Input formatting ---------------------------------------------

def _select_cited_chunks(evidence_package: dict, source_ids_cited: list[str]) -> list[dict]:
    """
    Filter evidence_package.sources to just the chunks the responder cited.
    The full sources list contains all retrieved chunks; we want only the
    ones the draft actually referenced.
    """
    sources = evidence_package.get("sources", []) or []
    cited_set = set(source_ids_cited or [])
    if not cited_set:
        return []
    out: list[dict] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        cid = src.get("chunk_id") or src.get("id")
        if cid in cited_set:
            out.append(src)
    return out


def _format_chunk(chunk: dict) -> str:
    cid = chunk.get("chunk_id") or chunk.get("id") or "?"
    text = chunk.get("text") or chunk.get("content") or ""
    return f"  [{cid}] {text}"


def _build_judge_user_message(
    review_text: str,
    draft_response: str,
    confidence: float,
    cited_chunks: list[dict],
) -> str:
    """
    Render the user message for the judge LLM. Stable, deterministic shape —
    if you change this, bump JUDGE_INPUT_VERSION above.
    """
    if cited_chunks:
        chunks_block = "\n".join(_format_chunk(c) for c in cited_chunks)
    else:
        chunks_block = "  (no cited chunks)"

    return (
        f"<review>{review_text}</review>\n\n"
        f"<draft_response>{draft_response}</draft_response>\n\n"
        f"<evidence_confidence>{confidence:.2f}</evidence_confidence>\n\n"
        f"<cited_chunks>\n{chunks_block}\n</cited_chunks>"
    )


# ---------- LLM call -----------------------------------------------------

def _call_judge_llm(user_message: str) -> tuple[dict, dict[str, int]]:
    """
    Call Claude to rule on a single (review, draft, cited chunks) triple.

    Returns:
        (parsed JSON dict, token counts dict)

    Raises:
        anthropic.APIError on API failure
        ValueError on empty/non-text response
        json.JSONDecodeError on unparseable JSON
        ValidationError on schema violation
    """
    try:
        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=JUDGE_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Judge API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Judge response was cut off (stop_reason='max_tokens'). "
            "Consider raising JUDGE_MAX_TOKENS in config."
        )

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    if not response.content:
        raise ValueError("Judge LLM returned empty response")
    block = response.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Expected a text response, got {type(block).__name__}")
    raw_text = block.text.strip()  # type: ignore[union-attr]

    try:
        data = parse_llm_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Judge response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    # Validate at the trust boundary.
    ruling = JudgeRuling.model_validate(data)
    return ruling.model_dump(), tokens


# ---------- Per-case scorer ----------------------------------------------

def judge_grounding(
    case: dict,
    record: dict,
    scored_per_case: dict,
    run_file_basename: str,
) -> dict | None:
    """
    Rule on one case. Returns None if the case is not flagged
    `low_conf_with_cite`. Otherwise returns:

      {
        "ruling":     "honest_hedge" | "misleading_fix_claim" | "unclear",
        "rationale":  str,
        "from_cache": bool,
      }
    """
    case_id = case["case_id"]
    case_scored = scored_per_case.get(case_id)
    if not case_scored:
        return None

    band = case_scored.get("grounding_band_compliance", {})
    if band.get("flag") != "low_conf_with_cite":
        return None

    if not record.get("ok") or not record.get("result"):
        return None

    result = record["result"]
    review_text = case.get("review_text") or case.get("review") or ""
    draft = result.get("drafted_response", "") or ""
    ep = result.get("evidence_package") or {}
    confidence = float(ep.get("confidence") or 0.0)
    cited_chunks = _select_cited_chunks(ep, result.get("source_ids_cited", []))

    user_message = _build_judge_user_message(review_text, draft, confidence, cited_chunks)
    cache_filename = _cache_key(run_file_basename, case_id, user_message)
    cache_path = JUDGE_CACHE_DIR / cache_filename

    cached = _cache_read(cache_path)
    if cached and "ruling" in cached:
        return {
            "ruling": cached["ruling"],
            "rationale": cached.get("rationale", ""),
            "from_cache": True,
        }

    try:
        ruling_data, tokens = _call_judge_llm(user_message)
    except (anthropic.APIError, ValueError, json.JSONDecodeError, ValidationError) as e:
        logger.error(f"Judge failed on {case_id}: {type(e).__name__}: {e}")
        return {
            "ruling": "unclear",
            "rationale": f"judge_error: {type(e).__name__}",
            "from_cache": False,
        }

    payload = {
        "ruling": ruling_data["ruling"],
        "rationale": ruling_data.get("rationale", ""),
        "cached_at": dt.datetime.now().isoformat(),
        "cache_key_components": {
            "run_file_basename": run_file_basename,
            "case_id": case_id,
            "judge_model": JUDGE_MODEL,
            "skill_sha8": _skill_sha(),
            "judge_input_version": JUDGE_INPUT_VERSION,
            "user_message_sha8": _sha8(user_message.encode("utf-8")),
        },
        "tokens": tokens,
    }
    _cache_write(cache_path, payload)

    return {
        "ruling": payload["ruling"],
        "rationale": payload["rationale"],
        "from_cache": False,
    }


# ---------- Batch scorer -------------------------------------------------

def judge_grounding_batch(
    cases: list[dict],
    records: list[dict],
    scored: dict,
    run_file_basename: str | None = None,
) -> dict:
    """
    Run the grounding judge over every case the deterministic scorer flagged
    as `low_conf_with_cite`. Returns:

      {
        "n_flagged":   int,
        "n_from_cache": int,
        "rulings": {"honest_hedge": int, "misleading_fix_claim": int, "unclear": int},
        "per_case": {case_id: {"ruling": ..., "rationale": ..., "from_cache": ...}, ...},
        "model": JUDGE_MODEL,
      }

    `run_file_basename` is the bare filename (no extension, no path) of the
    run file the records came from — used to scope the cache. If None, falls
    back to "unknown_run", which is fine for ad-hoc testing but defeats the
    "different runs get different cache entries" guarantee. Callers in the
    main eval flow should always pass it.
    """
    if run_file_basename is None:
        run_file_basename = "unknown_run"

    case_by_id = {c["case_id"]: c for c in cases}
    per_case_scored = scored.get("per_case", {})

    rulings = {"honest_hedge": 0, "misleading_fix_claim": 0, "unclear": 0}
    per_case: dict[str, dict] = {}
    n_from_cache = 0

    for record in records:
        if not record.get("ok"):
            continue
        case = case_by_id.get(record["case_id"])
        if case is None:
            continue
        result = judge_grounding(case, record, per_case_scored, run_file_basename)
        if result is None:
            continue
        per_case[record["case_id"]] = result
        rulings[result["ruling"]] = rulings.get(result["ruling"], 0) + 1
        if result["from_cache"]:
            n_from_cache += 1

    return {
        "n_flagged": len(per_case),
        "n_from_cache": n_from_cache,
        "rulings": rulings,
        "per_case": per_case,
        "model": JUDGE_MODEL,
    }
