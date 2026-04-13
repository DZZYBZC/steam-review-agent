"""
evals/scorers/pairwise.py — V1.5 LLM judge for revision-loop improvement.

Answers the question: **is the responder/critic revision loop earning
its tokens?** For every case that completed at least one revision iteration
and was approved by a human, this judge compares the iter-0 draft to the
final approved draft and rules:

  - revision_improved   — final draft is substantively better on at least
                          one of grounding, citations, hallucinations, tone,
                          or proposed_action correctness
  - revision_neutral    — substantively a no-op (cosmetic phrasing only)
  - revision_regressed  — final draft is worse on any dimension above
  - judge_error         — infrastructure-failure fallback (LLM/parse/validation)

Sibling of `evals/scorers/judge_action.py` and `evals/scorers/judge_grounding.py`.
Same cache key shape, same batch shape, same skill-loading pattern. Three
cleanly-cloned files now exist — that is the trigger for the `_judge_base.py`
extraction (Task #53), which should happen AFTER this lands, not during.

The eval run JSON only stores the FINAL drafted_response per record. The
iter-0 draft lives in the `audit_log_iterations` SQLite table and is fetched
in a single batch query at the top of `pairwise_batch` via
`pipeline.storage.get_iteration_drafts_batch`. Filtering by `run_id` (not
app_id+review_id) is critical: the same review can have multiple historical
runs and joining on (app_id, review_id) would pull stale iter-0 rows from a
previous run.

Pairwise input shape is deliberately narrow. The risky field would have been
`evidence_summary` because it's LLM-generated and varies in phrasing across
runs even when the underlying evidence is identical — which would create
spurious cache misses AND noisy rulings. Replaced with three stable fields:
cited_source_ids (deterministic), evidence_confidence (float), critic_reason
(structured enum). See `feedback_judge_input_shape_stability.md`.

Deterministic shortcut: before any LLM call, if iter-0 and final drafts
normalize-equal AND the actions match, the case is auto-labeled
revision_neutral with `"deterministic": True` and counted in `n_deterministic`.
This is NOT extended to revision_improved/regressed — those require the
judge's semantic read.

Error handling note: on judge LLM failure (API/parse/validation), this
scorer falls back to a dedicated `judge_error` ruling, NOT to a
substantive bucket like `revision_neutral`. Same isolation principle as
the other two judges.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

from config import (
    CLAUDE_API_KEY,
    DB_PATH,
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL,
    JUDGE_TEMPERATURE,
)
from pipeline.storage import get_iteration_drafts_batch
from utils import load_skill, parse_llm_json

logger = logging.getLogger(__name__)

# Bump this when anything OTHER than the skill file changes the rendered judge
# input or the output contract.
#
#   v1: initial release. Frozen 9-tag input shape: review, iter_0_draft,
#       iter_0_action, final_draft, final_action, critic_reason, critic_critique,
#       cited_source_ids, evidence_confidence. Output: {"ruling", "rationale"}
#       where ruling ∈ {revision_improved, revision_neutral, revision_regressed,
#       judge_error}. NO evidence_summary — that's the noise source.
JUDGE_INPUT_VERSION = 1

JUDGE_CACHE_DIR = Path(__file__).resolve().parents[1] / "judge_cache"
SKILL_NAME = "judge-pairwise"
SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME / "SKILL.md"

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)
SYSTEM_PROMPT = load_skill(SKILL_NAME)


# ---------- Pydantic ruling model (LOCAL — trust boundary) ----------------

class JudgePairwiseRuling(BaseModel):
    ruling: str = Field(
        ...,
        pattern="^(revision_improved|revision_neutral|revision_regressed|judge_error)$",
    )
    rationale: str = Field(default="")


# ---------- Cache key + I/O ----------------------------------------------

def _sha8(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:8]


def _skill_sha() -> str:
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
        logger.warning(f"Pairwise cache read failed for {cache_path.name}: {e}")
        return None


def _cache_write(cache_path: Path, payload: dict) -> None:
    JUDGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w") as f:
        json.dump(payload, f, indent=2)


# ---------- Input formatting ---------------------------------------------

def _build_judge_user_message(
    review_text: str,
    iter_0_draft: str,
    iter_0_action: str,
    final_draft: str,
    final_action: str,
    critic_reason: str,
    critic_critique: str,
    iter_0_cited: list[str],
    final_cited: list[str],
    confidence: float,
) -> str:
    """
    Frozen 9-tag user message. If you change this, bump JUDGE_INPUT_VERSION.

    Note the absence of evidence_summary: that field is LLM-generated by the
    investigator and drifts in phrasing across runs even when the underlying
    evidence is identical, creating spurious cache misses and noisier rulings.
    The judge gets the deterministic substitute fields (cited_source_ids,
    confidence, critic_reason) instead.
    """
    iter_0_cited_str = json.dumps(list(iter_0_cited or []))
    final_cited_str = json.dumps(list(final_cited or []))
    return (
        f"<review>{review_text}</review>\n\n"
        f"<iter_0_draft>{iter_0_draft}</iter_0_draft>\n\n"
        f"<iter_0_action>{iter_0_action}</iter_0_action>\n\n"
        f"<final_draft>{final_draft}</final_draft>\n\n"
        f"<final_action>{final_action}</final_action>\n\n"
        f"<critic_reason>{critic_reason}</critic_reason>\n\n"
        f"<critic_critique>{critic_critique}</critic_critique>\n\n"
        f"<cited_source_ids>iter_0={iter_0_cited_str}, final={final_cited_str}</cited_source_ids>\n\n"
        f"<evidence_confidence>{confidence:.2f}</evidence_confidence>"
    )


# ---------- Predicate (the gate) ------------------------------------------

def _should_judge(record: dict, iter_0_draft: str | None) -> bool:
    """
    Skip cases that errored, single-iteration cases (no revision happened),
    and cases where we can't reconstruct iter 0 (no audit_log_iterations row).
    Cases with iteration_count == 0 are the predicate negative controls.
    Missing iter_0 is a structural skip, NOT a judge_error — judge_error is
    reserved for infrastructure failures, per feedback_judge_error_isolated_bucket.md.
    """
    r = record.get("result") or {}
    return bool(
        record.get("ok")
        and r.get("stop_reason") == "human_approved"
        and (r.get("iteration_count") or 0) >= 1
        and iter_0_draft
        and r.get("drafted_response")
    )


# ---------- Deterministic shortcut ---------------------------------------

def _normalize(text: str) -> str:
    """Whitespace-collapsed, lowercased. Catches cosmetic-only revisions."""
    return " ".join((text or "").split()).lower()


# ---------- LLM call -----------------------------------------------------

def _call_judge_llm(user_message: str) -> tuple[dict, dict[str, int]]:
    try:
        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=JUDGE_TEMPERATURE,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Pairwise judge API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Pairwise judge response was cut off (stop_reason='max_tokens'). "
            "Consider raising JUDGE_MAX_TOKENS in config."
        )

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    if not response.content:
        raise ValueError("Pairwise judge LLM returned empty response")
    block = response.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Expected a text response, got {type(block).__name__}")
    raw_text = block.text.strip()  # type: ignore[union-attr]

    try:
        data = parse_llm_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Pairwise judge response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    ruling = JudgePairwiseRuling.model_validate(data)
    if ruling.ruling == "judge_error":
        raise ValueError("LLM returned reserved 'judge_error' string")
    return ruling.model_dump(), tokens


# ---------- Per-case scorer ----------------------------------------------

def judge_pairwise(
    case: dict,
    record: dict,
    iter_0_row: dict | None,
    run_file_basename: str,
) -> dict | None:
    """
    Rule on one case. Returns None if the case is not eligible for pairwise
    judging (single-iteration, errored, or missing iter-0 row). Otherwise:

      {
        "ruling":        "revision_improved" | "revision_neutral" | "revision_regressed" | "judge_error",
        "rationale":     str,
        "from_cache":    bool,
        "deterministic": bool,   # True iff labeled by the normalize-equal shortcut
      }
    """
    case_id = case["case_id"]
    iter_0_draft = (iter_0_row or {}).get("drafted_response") if iter_0_row else None
    if not _should_judge(record, iter_0_draft):
        return None

    result = record["result"]
    final_draft = result.get("drafted_response", "") or ""
    final_action = result.get("proposed_action", "") or ""
    iter_0_action = (iter_0_row or {}).get("proposed_action", "") or ""
    iter_0_cited = (iter_0_row or {}).get("source_ids_cited") or []
    final_cited = result.get("source_ids_cited") or []
    critic_reason = (iter_0_row or {}).get("reason_type", "") or ""
    critic_critique = (iter_0_row or {}).get("critique", "") or ""
    review_text = case.get("review_text") or case.get("review") or ""
    ep = result.get("evidence_package") or {}
    confidence = float(ep.get("confidence") or 0.0)

    # Deterministic shortcut: cosmetic-or-no change → revision_neutral with no
    # LLM call, no cache hit, no tokens spent. Counted in n_deterministic so
    # the snapshot diff can track how often the responder is being a no-op.
    if (
        _normalize(iter_0_draft or "") == _normalize(final_draft)
        and iter_0_action == final_action
    ):
        return {
            "ruling": "revision_neutral",
            "rationale": "deterministic: iter-0 and final drafts normalize-equal and actions match",
            "from_cache": False,
            "deterministic": True,
        }

    user_message = _build_judge_user_message(
        review_text,
        iter_0_draft or "",
        iter_0_action,
        final_draft,
        final_action,
        critic_reason,
        critic_critique,
        iter_0_cited,
        final_cited,
        confidence,
    )
    cache_filename = _cache_key(run_file_basename, case_id, user_message)
    cache_path = JUDGE_CACHE_DIR / cache_filename

    cached = _cache_read(cache_path)
    if cached and "ruling" in cached:
        return {
            "ruling": cached["ruling"],
            "rationale": cached.get("rationale", ""),
            "from_cache": True,
            "deterministic": False,
        }

    try:
        ruling_data, tokens = _call_judge_llm(user_message)
    except (anthropic.APIError, ValueError, json.JSONDecodeError, ValidationError) as e:
        logger.error(f"Pairwise judge failed on {case_id}: {type(e).__name__}: {e}")
        return {
            "ruling": "judge_error",
            "rationale": f"judge_error: {type(e).__name__}: {str(e)[:200]}",
            "from_cache": False,
            "deterministic": False,
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
        "deterministic": False,
    }


# ---------- Batch scorer -------------------------------------------------

def pairwise_batch(
    cases: list[dict],
    records: list[dict],
    run_file_basename: str | None = None,
) -> dict:
    """
    Run the pairwise judge over every multi-iteration approved case. Returns:

      {
        "n_judged":         int,
        "n_from_cache":     int,
        "n_deterministic":  int,   # subset of n_revision_neutral labeled by the shortcut
        "rulings": {
            "revision_improved": int, "revision_neutral": int,
            "revision_regressed": int, "judge_error": int,
        },
        "per_case": {case_id: {"ruling", "rationale", "from_cache", "deterministic"}, ...},
        "model": JUDGE_MODEL,
      }

    Top-of-batch DB fetch: collects every record's run_id (for records that
    pass the basic gate), calls `get_iteration_drafts_batch` ONCE, builds the
    in-memory map. Then iterates per-case using dict lookups only — no N+1
    DB hits.

    Cases with `iteration_count == 0` are the predicate negative controls and
    must NOT appear in `per_case`.
    """
    if run_file_basename is None:
        run_file_basename = "unknown_run"

    case_by_id = {c["case_id"]: c for c in cases}

    # Collect run_ids for records that even have a chance of being judged.
    # The full predicate is applied per-case below; here we just need a
    # candidate set narrow enough that the batch SQL is small but wide enough
    # not to miss any case the judge will actually evaluate.
    candidate_run_ids: list[str] = []
    for record in records:
        if not record.get("ok"):
            continue
        r = record.get("result") or {}
        if (r.get("iteration_count") or 0) >= 1 and r.get("run_id"):
            candidate_run_ids.append(r["run_id"])

    # Single batch SQL fetch — opens its own short-lived sqlite connection.
    iter_0_map: dict[str, dict] = {}
    if candidate_run_ids:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                iter_0_map = get_iteration_drafts_batch(conn, candidate_run_ids, iteration=0)
        except sqlite3.Error as e:
            logger.error(f"Pairwise batch DB fetch failed: {e}")
            iter_0_map = {}

    rulings = {
        "revision_improved": 0,
        "revision_neutral": 0,
        "revision_regressed": 0,
        "judge_error": 0,
    }
    per_case: dict[str, dict] = {}
    n_from_cache = 0
    n_deterministic = 0

    for record in records:
        if not record.get("ok"):
            continue
        case = case_by_id.get(record["case_id"])
        if case is None:
            continue
        run_id = (record.get("result") or {}).get("run_id") or ""
        iter_0_row = iter_0_map.get(run_id)
        result = judge_pairwise(case, record, iter_0_row, run_file_basename)
        if result is None:
            continue
        per_case[record["case_id"]] = result
        rulings[result["ruling"]] = rulings.get(result["ruling"], 0) + 1
        if result.get("from_cache"):
            n_from_cache += 1
        if result.get("deterministic"):
            n_deterministic += 1

    return {
        "n_judged": len(per_case),
        "n_from_cache": n_from_cache,
        "n_deterministic": n_deterministic,
        "rulings": rulings,
        "per_case": per_case,
        "model": JUDGE_MODEL,
    }
