"""
evals/scorers/judge_retrieval.py — Split retrieval judges.

Two LLM judges that share input plumbing but answer different questions:

  - judge_evidence_vs_gold  (kind="gold")  : Pure retrieval signal.
        Inputs: review, ideal_action, case_notes, retrieved_chunks.
        Question: "could an ideal responder produce the gold-intent answer
        from this evidence pool alone?"
        Never sees the draft. Never penalizes the responder.

  - judge_evidence_vs_draft (kind="draft") : Joint retrieval+drafting signal.
        Inputs: review, draft_response, retrieved_chunks.
        Question: "do these chunks actually support the claims the draft makes?"
        Never sees the gold answer.

The delta between the two judges is the diagnostic — see reporter.py's
disagreement bucket section for the strict bucket definitions.

Same cache + cache-key + judge_error handling discipline as
judge_grounding.py and judge_action.py. The judge_kind component in the
cache filename guarantees the two judges can never collide on disk even
when they run against the same case in the same run file.

Cache directory: evals/judge_retrieval_cache/  (sibling of judge_cache/,
kept separate so grep-by-judge stays clean and so an accidental delete of
one cache dir does not blow away the other family of judges).

If anything that affects the rendered judge input changes (input format,
chunk filter, output schema), bump JUDGE_INPUT_VERSION below.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Literal

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

# Bump when anything OTHER than the skill files changes the rendered judge
# input or the output contract — e.g., _build_*_user_message() shape, the
# chunk filter, the JSON output schema. Cache key includes this constant
# so a bump invalidates every cached entry across BOTH judges.
#
#   v1: initial release. Gold inputs: review, ideal_action, case_notes,
#       retrieved_chunks. Draft inputs: review, draft_response,
#       retrieved_chunks. Output: {"ruling", "rationale"} where ruling ∈
#       {supports, partially_supports, does_not_support}.
JUDGE_INPUT_VERSION = 1

# Retrieval judges quote chunk text in the rationale, which can overflow the
# global JUDGE_MAX_TOKENS=300. 600 buys headroom without affecting cache keys
# (max_tokens is not in the cache key — it only affects whether the response truncates).
RETRIEVAL_JUDGE_MAX_TOKENS = 600

JUDGE_CACHE_DIR = Path(__file__).resolve().parents[1] / "judge_retrieval_cache"

GOLD_SKILL_NAME = "judge-evidence-vs-gold"
DRAFT_SKILL_NAME = "judge-evidence-vs-draft"
GOLD_SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / GOLD_SKILL_NAME / "SKILL.md"
DRAFT_SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / DRAFT_SKILL_NAME / "SKILL.md"

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)
GOLD_SYSTEM_PROMPT = load_skill(GOLD_SKILL_NAME)
DRAFT_SYSTEM_PROMPT = load_skill(DRAFT_SKILL_NAME)


# Local trust-boundary model — judge never enters the agent path.
# judge_error is in the regex so the success and fallback branches share
# one validator path; the LLM itself is not allowed to emit it (see
# _call_judge_llm).

class RetrievalJudgeRuling(BaseModel):
    ruling: str = Field(
        ...,
        pattern="^(supports|partially_supports|does_not_support|judge_error)$",
    )
    rationale: str = Field(default="")


# ---------- Cache key + I/O ----------------------------------------------

def _sha8(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:8]


def _skill_sha(skill_path: Path) -> str:
    return _sha8(skill_path.read_bytes())


def _cache_key(
    run_file_basename: str,
    case_id: str,
    judge_kind: Literal["gold", "draft"],
    skill_path: Path,
    user_message: str,
) -> str:
    """
    Cache filename. Every component that affects the ruling is in the key:

      1. run_file_basename — different runs have different drafts/evidence
      2. case_id           — obvious
      3. judge_kind        — gold and draft must NEVER share a cache entry
                              even when run_file/case/model line up
      4. JUDGE_MODEL       — model swap is a behavior change
      5. skill_sha8        — prompt text change (per-judge skill file)
      6. JUDGE_INPUT_VERSION — manual bump for input/output shape changes
      7. user_message_sha  — belt-and-suspenders: catches changes the dev
                              forgot to bump JUDGE_INPUT_VERSION for
    """
    skill_sha = _skill_sha(skill_path)
    user_msg_sha = _sha8(user_message.encode("utf-8"))
    model_safe = JUDGE_MODEL.replace("/", "_")
    return (
        f"{run_file_basename}__{case_id}__{judge_kind}__{model_safe}"
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

def _select_relevant_chunks(evidence_package: dict) -> list[dict]:
    """
    Return every chunk in the post-filter (relevant) pool, with full text.
    The judges score the relevant_ids set, NOT the cited subset — they're
    asking about retrieval/filter quality, not citation behavior.
    """
    sources = evidence_package.get("sources", []) or []
    relevant_set = set(evidence_package.get("relevant_ids") or [])
    if not relevant_set:
        return []
    out: list[dict] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        cid = src.get("chunk_id") or src.get("id")
        if cid in relevant_set:
            out.append(src)
    return out


def _format_chunk(chunk: dict) -> str:
    cid = chunk.get("chunk_id") or chunk.get("id") or "?"
    text = chunk.get("text") or chunk.get("content") or ""
    return f"  [{cid}] {text}"


def _format_chunks_block(chunks: list[dict]) -> str:
    if not chunks:
        return "  (no cited chunks)"
    return "\n".join(_format_chunk(c) for c in chunks)


def _build_gold_user_message(
    review_text: str,
    ideal_action: str,
    case_notes: str,
    chunks: list[dict],
) -> str:
    """Stable shape — bump JUDGE_INPUT_VERSION if you edit this."""
    return (
        f"<review>{review_text}</review>\n\n"
        f"<ideal_action>{ideal_action}</ideal_action>\n\n"
        f"<case_notes>{case_notes}</case_notes>\n\n"
        f"<retrieved_chunks>\n{_format_chunks_block(chunks)}\n</retrieved_chunks>"
    )


def _build_draft_user_message(
    review_text: str,
    draft_response: str,
    chunks: list[dict],
) -> str:
    """Stable shape — bump JUDGE_INPUT_VERSION if you edit this."""
    return (
        f"<review>{review_text}</review>\n\n"
        f"<draft_response>{draft_response}</draft_response>\n\n"
        f"<retrieved_chunks>\n{_format_chunks_block(chunks)}\n</retrieved_chunks>"
    )


# ---------- LLM call -----------------------------------------------------

def _call_judge_llm(
    system_prompt: str,
    user_message: str,
) -> tuple[dict, dict[str, int]]:
    """
    Call Claude on a single (system, user) pair. Mirrors judge_action's
    error contract: raises on API/parse/validation failure; the per-case
    function below catches and falls back to judge_error.
    """
    try:
        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=RETRIEVAL_JUDGE_MAX_TOKENS,
            temperature=JUDGE_TEMPERATURE,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Retrieval judge API call failed: {e}")
        raise

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Retrieval judge response was cut off (stop_reason='max_tokens'). "
            "Consider raising JUDGE_MAX_TOKENS in config."
        )

    tokens = {
        "input": response.usage.input_tokens,
        "output": response.usage.output_tokens,
    }

    if not response.content:
        raise ValueError("Retrieval judge LLM returned empty response")
    block = response.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Expected a text response, got {type(block).__name__}")
    raw_text = block.text.strip()  # type: ignore[union-attr]

    try:
        data = parse_llm_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Retrieval judge response is not valid JSON: {e}")
        logger.error(f"Raw response was: {raw_text[:500]}")
        raise

    ruling = RetrievalJudgeRuling.model_validate(data)
    if ruling.ruling == "judge_error":
        raise ValueError("LLM returned reserved 'judge_error' string")
    return ruling.model_dump(), tokens


# ---------- Per-case predicates ------------------------------------------

def _gold_predicate(case: dict, result: dict) -> bool:
    """
    Gold judge runs iff:
      - the agent surfaced a non-empty post-filter pool (relevant_ids), AND
      - the case has either notes OR an ideal_action to score against.
    """
    ep = result.get("evidence_package") or {}
    if not (ep.get("relevant_ids") or []):
        return False
    return bool((case.get("notes") or "").strip() or (case.get("ideal_action") or "").strip())


def _draft_predicate(case: dict, result: dict) -> bool:
    """
    Draft judge runs iff:
      - the agent surfaced a non-empty post-filter pool, AND
      - the responder produced a non-empty drafted_response.
    """
    ep = result.get("evidence_package") or {}
    if not (ep.get("relevant_ids") or []):
        return False
    return bool((result.get("drafted_response") or "").strip())


# ---------- Per-case scorers ---------------------------------------------

def _judge_one(
    judge_kind: Literal["gold", "draft"],
    case_id: str,
    user_message: str,
    system_prompt: str,
    skill_path: Path,
    run_file_basename: str,
) -> dict:
    """
    Cache-aware single-call wrapper. Returns:
      {"ruling": ..., "rationale": ..., "from_cache": bool}
    """
    cache_filename = _cache_key(
        run_file_basename, case_id, judge_kind, skill_path, user_message
    )
    cache_path = JUDGE_CACHE_DIR / cache_filename

    cached = _cache_read(cache_path)
    if cached and "ruling" in cached:
        return {
            "ruling": cached["ruling"],
            "rationale": cached.get("rationale", ""),
            "from_cache": True,
        }

    try:
        ruling_data, tokens = _call_judge_llm(system_prompt, user_message)
    except (anthropic.APIError, ValueError, json.JSONDecodeError, ValidationError) as e:
        # judge_error stays isolated — never collapsed into a substantive ruling.
        logger.error(
            f"Retrieval judge ({judge_kind}) failed on {case_id}: "
            f"{type(e).__name__}: {e}"
        )
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
            "judge_kind": judge_kind,
            "judge_model": JUDGE_MODEL,
            "skill_sha8": _skill_sha(skill_path),
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


def judge_evidence_vs_gold(
    case: dict,
    record: dict,
    run_file_basename: str,
) -> dict | None:
    """
    Returns None if the gold predicate is unmet. Otherwise returns the
    standard {ruling, rationale, from_cache} dict.
    """
    if not record.get("ok") or not record.get("result"):
        return None
    result = record["result"]
    if not _gold_predicate(case, result):
        return None

    review_text = case.get("review_text") or case.get("review") or ""
    ideal_action = case.get("ideal_action") or ""
    case_notes = case.get("notes") or ""
    ep = result.get("evidence_package") or {}
    chunks = _select_relevant_chunks(ep)

    user_message = _build_gold_user_message(review_text, ideal_action, case_notes, chunks)
    return _judge_one(
        "gold",
        case["case_id"],
        user_message,
        GOLD_SYSTEM_PROMPT,
        GOLD_SKILL_PATH,
        run_file_basename,
    )


def judge_evidence_vs_draft(
    case: dict,
    record: dict,
    run_file_basename: str,
) -> dict | None:
    """
    Returns None if the draft predicate is unmet. Otherwise returns the
    standard {ruling, rationale, from_cache} dict.
    """
    if not record.get("ok") or not record.get("result"):
        return None
    result = record["result"]
    if not _draft_predicate(case, result):
        return None

    review_text = case.get("review_text") or case.get("review") or ""
    draft = result.get("drafted_response", "") or ""
    ep = result.get("evidence_package") or {}
    chunks = _select_relevant_chunks(ep)

    user_message = _build_draft_user_message(review_text, draft, chunks)
    return _judge_one(
        "draft",
        case["case_id"],
        user_message,
        DRAFT_SYSTEM_PROMPT,
        DRAFT_SKILL_PATH,
        run_file_basename,
    )


# ---------- Batch scorers -------------------------------------------------

def _empty_rulings() -> dict[str, int]:
    return {
        "supports": 0,
        "partially_supports": 0,
        "does_not_support": 0,
        "judge_error": 0,
    }


def _run_batch(
    cases: list[dict],
    records: list[dict],
    run_file_basename: str | None,
    judge_kind: Literal["gold", "draft"],
) -> dict:
    """
    Shared batch loop for both judges. Counts predicate-eligible cases
    separately from judged cases so the reporter can surface the
    drop-from-eligible-to-judged chain that the plan calls for.
    """
    if run_file_basename is None:
        run_file_basename = "unknown_run"

    case_by_id = {c["case_id"]: c for c in cases}
    rulings = _empty_rulings()
    per_case: dict[str, dict] = {}
    n_predicate_eligible = 0
    n_from_cache = 0

    judge_fn = (
        judge_evidence_vs_gold if judge_kind == "gold" else judge_evidence_vs_draft
    )

    for record in records:
        if not record.get("ok"):
            continue
        case = case_by_id.get(record["case_id"])
        if case is None:
            continue
        result = record.get("result") or {}
        # Predicate-eligibility is tracked even when judge returns None so
        # the reporter can show "predicate=P/M" alongside "n_judged=K".
        predicate_fn = _gold_predicate if judge_kind == "gold" else _draft_predicate
        if predicate_fn(case, result):
            n_predicate_eligible += 1
        ruling = judge_fn(case, record, run_file_basename)
        if ruling is None:
            continue
        per_case[case["case_id"]] = ruling
        rulings[ruling["ruling"]] = rulings.get(ruling["ruling"], 0) + 1
        if ruling["from_cache"]:
            n_from_cache += 1

    return {
        "judge_kind": judge_kind,
        "n_predicate_eligible": n_predicate_eligible,
        "n_judged": len(per_case),
        "n_from_cache": n_from_cache,
        "rulings": rulings,
        "per_case": per_case,
        "model": JUDGE_MODEL,
    }


def judge_evidence_vs_gold_batch(
    cases: list[dict],
    records: list[dict],
    run_file_basename: str | None = None,
) -> dict:
    """
    Run the gold-judge over every case where the predicate fires
    (non-empty relevant_ids AND non-empty notes-or-ideal_action).
    """
    return _run_batch(cases, records, run_file_basename, "gold")


def judge_evidence_vs_draft_batch(
    cases: list[dict],
    records: list[dict],
    run_file_basename: str | None = None,
) -> dict:
    """
    Run the draft-judge over every case where the predicate fires
    (non-empty relevant_ids AND non-empty drafted_response).
    """
    return _run_batch(cases, records, run_file_basename, "draft")
