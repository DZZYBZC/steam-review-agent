"""
evals/scorers/judge_action.py — V1.5 LLM judge for the `wrong_action_severity`
deterministic failure mode.

The deterministic scorer (`action_correctness`) can detect THAT a case
mispredicted the action (ideal != predicted), and that count rolls up as
`failure_modes["wrong_action_severity"]`. But it cannot tell WHICH KIND of
wrong:

  - over_escalation       — agent went investigate/escalate when no_action/monitor was right
  - missed_escalation     — agent went no_action/monitor when investigate/escalate was right
  - category_drift        — adjacent ladder swap with no clear direction failure
  - tolerable_disagreement — predicted action is genuinely defensible

This module asks Claude to make that ruling, one case at a time.

Sibling of `evals/scorers/judge_grounding.py`. Same cache key shape, same
batch shape, same skill-loading pattern. The two cleanly-cloned files are
the artifact that will make the right shape of a future `_judge_base.py`
extraction visible — DO NOT extract until both judges exist and are
exercised.

Caching: results are cached on disk under `evals/judge_cache/`. The cache
key covers every input that affects the ruling — see _cache_key() for the
full list. The grounding judge writes to the same dir; collision is
prevented by the skill_sha8 component (judge-action and judge-grounding
have different skill files, hence different sha8 values).

If anything that affects the rendered judge input changes (input format
code, chunk filter, output schema), bump JUDGE_INPUT_VERSION below.

Error handling note: on judge LLM failure (API/parse/validation), this
scorer falls back to a dedicated `judge_error` ruling, NOT to a
substantive bucket like `tolerable_disagreement`. Collapsing infrastructure
failures into a semantic bucket would silently undercount real action
problems whenever the judge misfires. `judge_error` is reported separately
in the snapshot and the reporter so misfires are visible immediately.
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
#   v1: initial release. Inputs: review, draft, ideal_action, predicted_action,
#       evidence_confidence, evidence_summary, cited chunks (id + text).
#       Output: {"ruling", "rationale"} where ruling ∈ {over_escalation,
#       missed_escalation, category_drift, tolerable_disagreement, judge_error}.
JUDGE_INPUT_VERSION = 1

JUDGE_CACHE_DIR = Path(__file__).resolve().parents[1] / "judge_cache"
SKILL_NAME = "judge-action"
SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME / "SKILL.md"

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)
SYSTEM_PROMPT = load_skill(SKILL_NAME)


# Local trust-boundary model — judge never enters the agent path.
# judge_error is in the regex so the success and fallback branches share one
# validator path; the LLM itself is not allowed to emit it (see _call_judge_llm).

class JudgeActionRuling(BaseModel):
    ruling: str = Field(
        ...,
        pattern="^(over_escalation|missed_escalation|category_drift|tolerable_disagreement|judge_error)$",
    )
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
    """See judge_grounding._cache_key for the component list."""
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
    Identical to judge_grounding._select_cited_chunks — kept duplicated on
    purpose so the two judge files stay independently auditable.
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
    ideal_action: str,
    predicted_action: str,
    confidence: float,
    evidence_summary: str,
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
        f"<ideal_action>{ideal_action}</ideal_action>\n\n"
        f"<predicted_action>{predicted_action}</predicted_action>\n\n"
        f"<evidence_confidence>{confidence:.2f}</evidence_confidence>\n\n"
        f"<evidence_summary>{evidence_summary}</evidence_summary>\n\n"
        f"<cited_chunks>\n{chunks_block}\n</cited_chunks>"
    )


# ---------- Predicate (the gate) ------------------------------------------

def _should_judge(case_scored: dict) -> bool:
    """
    The judge runs on exactly the cases the deterministic scorer counts as
    `wrong_action_severity`. This predicate is byte-identical to the one in
    `evals/scorers/deterministic.py:_failure_mode_section` — keeping them in
    sync by construction is the whole point. If the deterministic predicate
    ever changes, change this one in lockstep.
    """
    act = case_scored.get("action_correctness", {})
    return bool(
        act.get("applicable", True)
        and not act.get("correct")
        and act.get("ideal")
        and act.get("predicted")
    )


# ---------- LLM call -----------------------------------------------------

def _call_judge_llm(user_message: str) -> tuple[dict, dict[str, int]]:
    """
    Call Claude to rule on a single (review, draft, ideal, predicted, evidence)
    bundle.

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

    ruling = JudgeActionRuling.model_validate(data)
    if ruling.ruling == "judge_error":
        raise ValueError("LLM returned reserved 'judge_error' string")
    return ruling.model_dump(), tokens


# ---------- Per-case scorer ----------------------------------------------

def judge_action(
    case: dict,
    record: dict,
    scored_per_case: dict,
    run_file_basename: str,
) -> dict | None:
    """
    Rule on one case. Returns None if the case is not flagged
    `wrong_action_severity`. Otherwise returns:

      {
        "ruling":     "over_escalation" | "missed_escalation" | "category_drift" |
                      "tolerable_disagreement" | "judge_error",
        "rationale":  str,
        "from_cache": bool,
      }

    `judge_error` is the fallback for LLM call failures — see module
    docstring for why it stays isolated from the four semantic buckets.
    """
    case_id = case["case_id"]
    case_scored = scored_per_case.get(case_id)
    if not case_scored:
        return None

    if not _should_judge(case_scored):
        return None

    if not record.get("ok") or not record.get("result"):
        return None

    result = record["result"]
    review_text = case.get("review_text") or case.get("review") or ""
    draft = result.get("drafted_response", "") or ""
    ep = result.get("evidence_package") or {}
    confidence = float(ep.get("confidence") or 0.0)
    evidence_summary = ep.get("summary") or ""
    cited_chunks = _select_cited_chunks(ep, result.get("source_ids_cited", []))

    ideal = case_scored["action_correctness"].get("ideal", "")
    predicted = case_scored["action_correctness"].get("predicted", "")

    user_message = _build_judge_user_message(
        review_text, draft, ideal, predicted, confidence, evidence_summary, cited_chunks
    )
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
        # Judge errors get their OWN bucket. Do not collapse into a substantive
        # ruling — that would silently convert "the judge infrastructure failed"
        # into "the disagreement was probably fine," undercounting real action
        # problems. The reporter surfaces n_judge_error > 0 with a warning.
        logger.error(f"Judge failed on {case_id}: {type(e).__name__}: {e}")
        return {
            "ruling": "judge_error",
            "rationale": f"judge_error: {type(e).__name__}: {str(e)[:200]}",
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

def judge_action_batch(
    cases: list[dict],
    records: list[dict],
    scored: dict,
    run_file_basename: str | None = None,
) -> dict:
    """
    Run the action judge over every case the deterministic scorer flagged
    as `wrong_action_severity`. Returns:

      {
        "n_flagged":   int,
        "n_from_cache": int,
        "rulings": {
            "over_escalation": int, "missed_escalation": int,
            "category_drift": int, "tolerable_disagreement": int,
            "judge_error": int,
        },
        "per_case": {case_id: {"ruling": ..., "rationale": ..., "from_cache": ...}, ...},
        "model": JUDGE_MODEL,
      }

    `judge_error` is reported alongside the four semantic buckets so a
    misfiring judge is visible immediately and never gets silently absorbed
    into a substantive ruling count.

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

    rulings = {
        "over_escalation": 0,
        "missed_escalation": 0,
        "category_drift": 0,
        "tolerable_disagreement": 0,
        "judge_error": 0,
    }
    per_case: dict[str, dict] = {}
    n_from_cache = 0

    for record in records:
        if not record.get("ok"):
            continue
        case = case_by_id.get(record["case_id"])
        if case is None:
            continue
        result = judge_action(case, record, per_case_scored, run_file_basename)
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
