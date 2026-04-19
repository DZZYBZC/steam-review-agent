"""
agent/planner_cm.py — CM (Community Manager) ReAct-style planner.

The planner converts a natural-language goal into a terminal action by driving
a tool-use loop with six tools across three categories:

  Information-gathering (read-only, cheap):
    - count_matching_reviews(filter)
    - inspect_reviews(filter, k)

  Corpus expansion (slow, single-call budget per kind):
    - fetch_game_reviews(app_id, max_reviews)
    - fetch_game_patch_notes(app_id, max_items)

  Terminal actions (each ends the loop):
    - draft_responses_for_batch(filter, synthesis_instruction, uncertain, concerns)
    - reject_goal(reason)

Mirrors agent/nodes/investigator.py structurally — synchronous while-loop,
dual-budget design, JSON-extraction tolerance, per-call JSONL logging.

The deterministic CM-level human gate is decided by cm_runner.run_cm based on
`uncertain` + `concerns` plus filter shape — NOT by the planner itself
(see CLAUDE.md: coordinator is plain Python because branching is knowable).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import anthropic

from config import (
    CLAUDE_API_KEY,
    CM_PLANNER_FETCH_PATCH_NOTES_HARD_CAP,
    CM_PLANNER_FETCH_REVIEWS_HARD_CAP,
    CM_PLANNER_INSPECT_PREVIEW_CHARS,
    CM_PLANNER_MAX_FETCH_CALLS,
    CM_PLANNER_MAX_INFO_CALLS,
    CM_PLANNER_MAX_TOKENS,
    CM_PLANNER_MAX_TOTAL_TURNS,
    CM_PLANNER_MODEL,
    CM_PLANNER_TEMPERATURE,
    DB_PATH,
    REVIEW_CATEGORIES,
)
from utils import escape_xml, load_skill

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_skill("plan-cm-goal")

_PLANNER_LOG_DIR = Path(__file__).resolve().parent.parent / "evals" / "logs"


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

# Reusable filter sub-schema (no `limit` — counting/inspecting doesn't select).
_FILTER_PROPS_NO_LIMIT = {
    "app_id": {"type": ["string", "null"]},
    "category": {"type": ["string", "null"], "enum": list(REVIEW_CATEGORIES) + [None]},
    "voted_up": {"type": ["boolean", "null"]},
    "since_days": {"type": ["integer", "null"], "minimum": 0},
}

# For draft_responses_for_batch — same as above plus a required `limit`.
_FILTER_PROPS_WITH_LIMIT = {
    **_FILTER_PROPS_NO_LIMIT,
    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
}

TOOLS: list[dict] = [
    {
        "name": "count_matching_reviews",
        "description": (
            "Count reviews matching a filter. Use FIRST before any other action — "
            "this is your probe to discover whether data exists and how big the "
            "candidate pool is. Returns {count, app_id_known} where app_id_known=False "
            "means the app_id has zero reviews in the DB at all (signals you may need "
            "to fetch). Cheap (no LLM, just SQL). Up to 3 calls per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": _FILTER_PROPS_NO_LIMIT,
            "required": [],
        },
    },
    {
        "name": "inspect_reviews",
        "description": (
            "Fetch up to k sample review previews matching a filter. Use to "
            "sanity-check that the filter actually matches the user's intent "
            "before committing to drafting. Returns previews truncated to "
            f"{CM_PLANNER_INSPECT_PREVIEW_CHARS} chars per review. Up to 2 calls per run, k clamped to [1, 5]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {"type": "object", "properties": _FILTER_PROPS_NO_LIMIT, "required": []},
                "k": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["filter", "k"],
        },
    },
    {
        "name": "lookup_app_by_name",
        "description": (
            "Resolve a game NAME to its Steam app_id by querying Steam's storefront "
            "search. Use FIRST whenever the user names a game by title (e.g. "
            "\"Baldur's Gate 3\", \"Cyberpunk\", \"Monster Hunter Wilds\") instead of "
            "by id, before any count or fetch call. Returns ranked candidates with "
            "app_id, name, and type (\"app\" / \"dlc\" / \"music\" / etc.). The first "
            "result is usually the canonical match for popular games — but if "
            "exact_match=False, the user's name was ambiguous; surface that as a "
            "concern when committing. Up to 2 calls per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
            },
            "required": ["name"],
        },
    },
    {
        "name": "fetch_game_reviews",
        "description": (
            "Fetch fresh reviews from Steam for a game not currently in the DB. "
            "Slow (30-90 seconds for 200 reviews including classification). "
            "Use ONLY when count_matching_reviews returned app_id_known=False or "
            "the data is older than the user needs. max_reviews is clamped to "
            f"[20, {CM_PLANNER_FETCH_REVIEWS_HARD_CAP}]. One call per app_id per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "max_reviews": {
                    "type": "integer",
                    "minimum": 20,
                    "maximum": CM_PLANNER_FETCH_REVIEWS_HARD_CAP,
                },
            },
            "required": ["app_id", "max_reviews"],
        },
    },
    {
        "name": "fetch_game_patch_notes",
        "description": (
            "Fetch and index patch notes from the Steam News API for a game. "
            "Necessary for the investigator sub-agent to ground its responses. "
            "Run alongside fetch_game_reviews when working with a cold game. "
            f"max_items clamped to [10, {CM_PLANNER_FETCH_PATCH_NOTES_HARD_CAP}]. One call per app_id per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1},
                "max_items": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": CM_PLANNER_FETCH_PATCH_NOTES_HARD_CAP,
                },
            },
            "required": ["app_id", "max_items"],
        },
    },
    {
        "name": "draft_responses_for_batch",
        "description": (
            "TERMINAL action — commit to drafting responses for the batch. The orchestrator "
            "will fire the investigator→responder→critic→gate sub-agent loop on each "
            "matching review. Set uncertain=True and populate concerns[] when the goal is "
            "ambiguous, the filter has no app_id, you triggered a fetch, the count is "
            "large, or you're not confident the filter matches user intent. The "
            "orchestrator decides whether to gate the user — your job is to *describe* "
            "uncertainty, not *decide* whether to ask. Calling this ends the planning loop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "properties": _FILTER_PROPS_WITH_LIMIT,
                    "required": ["limit"],
                },
                "synthesis_instruction": {"type": "string"},
                "uncertain": {"type": "boolean"},
                "concerns": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["filter", "synthesis_instruction", "uncertain", "concerns"],
        },
    },
    {
        "name": "reject_goal",
        "description": (
            "TERMINAL action — refuse to plan a batch. Use when input is gibberish, "
            "contradictory, unactionable, or when the user is asking for something "
            "outside the CM scope (multi-game requests, etc.). Reason must be ≥20 chars "
            "and concretely describe the problem. Calling this ends the planning loop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "minLength": 20}},
            "required": ["reason"],
        },
    },
]


# ---------------------------------------------------------------------------
# Planner result + context
# ---------------------------------------------------------------------------

@dataclass
class PlannerResult:
    """Final output of CMPlanner.plan(). Consumed by cm_runner.run_cm."""
    terminal_action: Literal["execute_batch", "reject"]
    filter: dict | None = None              # only for execute_batch
    synthesis_instruction: str = ""         # only for execute_batch
    uncertain: bool = False                 # only for execute_batch
    concerns: list[str] = field(default_factory=list)  # only for execute_batch
    reason: str = ""                        # only for reject
    fetch_calls_made: dict[str, int] = field(default_factory=dict)  # per-app counter
    # When lookup_app_by_name found ≥2 in-DB matches, this carries the candidate
    # list so cm_runner can surface a disambiguation picker on the human gate.
    # Empty list = no disambiguation needed (single in-DB match, none, or lookup
    # not called).
    app_id_alternatives: list[dict] = field(default_factory=list)
    tool_call_log: list[dict] = field(default_factory=list)
    runaway: bool = False                   # True if MAX_TOTAL_TURNS exhausted


@dataclass
class PlannerContext:
    """Tracks budgets and side-effect state across one planner.plan() call."""
    info_calls_used: int = 0
    fetch_calls_used: int = 0
    fetch_calls_made: dict[str, int] = field(default_factory=dict)  # app_id -> count, by tool kind sum
    fetched_reviews_for: set[str] = field(default_factory=set)
    fetched_patch_notes_for: set[str] = field(default_factory=set)
    # Populated by _handle_lookup_app_by_name when ≥2 in-DB matches surface;
    # passed through to PlannerResult so the orchestrator can show a picker.
    app_id_alternatives: list[dict] = field(default_factory=list)
    event_emitter: Callable[[str, dict], None] | None = None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _build_filter_clauses(filter_dict: dict) -> tuple[list[str], list[Any]]:
    """Build SQLite WHERE clauses + params from a filter dict.

    Mirrors cm_runner._select_candidates' clause shape but is a pure helper that
    both count_matching_reviews and inspect_reviews can reuse.
    """
    clauses: list[str] = ["r.is_near_duplicate = 0", "LENGTH(r.review_text) >= 40"]
    params: list[Any] = []

    if filter_dict.get("app_id"):
        clauses.append("r.app_id = ?")
        params.append(filter_dict["app_id"])
    if filter_dict.get("category"):
        clauses.append("c.primary_category = ?")
        params.append(filter_dict["category"])
    if filter_dict.get("voted_up") is not None:
        clauses.append("r.voted_up = ?")
        params.append(int(bool(filter_dict["voted_up"])))
    if filter_dict.get("since_days") is not None:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=int(filter_dict["since_days"]))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        clauses.append("r.timestamp >= ?")
        params.append(cutoff)
    return clauses, params


def _handle_count_matching_reviews(input_payload: dict, ctx: PlannerContext) -> dict:
    """SQL count + app_id_known check. Negligible latency."""
    filt = {k: input_payload.get(k) for k in ("app_id", "category", "voted_up", "since_days")}
    clauses, params = _build_filter_clauses(filt)
    sql = f"""
        SELECT COUNT(*) FROM reviews r
        LEFT JOIN classifications c ON r.review_id = c.review_id
        WHERE {" AND ".join(clauses)}
    """
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(sql, params).fetchone()[0]

    app_id_known = True
    if filt.get("app_id"):
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT 1 FROM reviews WHERE app_id = ? LIMIT 1", (filt["app_id"],),
            ).fetchone()
            app_id_known = row is not None

    return {"count": int(count), "app_id_known": app_id_known}


def _handle_lookup_app_by_name(input_payload: dict, ctx: PlannerContext) -> dict:
    """Resolve a game name to ranked app_id candidates via Steam storefront search.

    Each candidate is also cross-referenced against the local reviews DB:
    `in_local_db` (bool) tells the planner whether we already have data for it,
    and `n_local_reviews` gives the review count. When >1 candidates are
    in-DB, the orchestrator surfaces a picker on the human gate so the user
    can disambiguate.
    """
    from pipeline.steam_app_index import search_apps

    name = str(input_payload.get("name", "")).strip()
    if not name:
        return {"candidates": [], "exact_match": False, "error": "empty name"}

    try:
        candidates = search_apps(name, k=5)
    except Exception as e:  # noqa: BLE001
        logger.exception("lookup_app_by_name: storefront search failed for %r", name)
        return {"candidates": [], "exact_match": False,
                "error": f"Steam storefront search unreachable: {type(e).__name__}: {e}"}

    if not candidates:
        return {"candidates": [], "exact_match": False, "error": None,
                "note": "no Steam apps matched the query"}

    # Cross-reference each Steam candidate against the local reviews DB so the
    # planner (and downstream orchestrator) can disambiguate among in-DB matches.
    candidate_ids = [c["app_id"] for c in candidates]
    placeholders = ",".join("?" * len(candidate_ids))
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT app_id, COUNT(*) FROM reviews WHERE app_id IN ({placeholders}) GROUP BY app_id",
            candidate_ids,
        ).fetchall()
    db_counts = {row[0]: int(row[1]) for row in rows}

    enriched = []
    for c in candidates:
        n_reviews = db_counts.get(c["app_id"], 0)
        enriched.append({
            **c,
            "in_local_db": n_reviews > 0,
            "n_local_reviews": n_reviews,
        })

    in_db_matches = [c for c in enriched if c["in_local_db"]]
    # Capture alternatives for the orchestrator's disambiguation picker.
    # Only fires when ≥2 in-DB candidates — otherwise there's nothing to disambiguate.
    if len(in_db_matches) >= 2:
        ctx.app_id_alternatives = in_db_matches

    # exact_match: top result's name matches the query case-insensitively.
    top_name = enriched[0]["name"].strip().lower()
    exact_match = top_name == name.lower()
    return {
        "candidates": enriched,
        "exact_match": exact_match,
        "n_in_local_db": len(in_db_matches),
        "error": None,
    }


def _handle_inspect_reviews(input_payload: dict, ctx: PlannerContext) -> dict:
    """SQL select + truncate previews. Returns at most k rows."""
    filt = input_payload.get("filter", {})
    k = max(1, min(5, int(input_payload.get("k", 3))))
    clauses, params = _build_filter_clauses(filt)
    sql = f"""
        SELECT r.review_id, r.voted_up, r.review_text, r.timestamp,
               c.primary_category
        FROM reviews r
        LEFT JOIN classifications c ON r.review_id = c.review_id
        WHERE {" AND ".join(clauses)}
        ORDER BY r.timestamp DESC
        LIMIT ?
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params + [k]).fetchall()

    return {"reviews": [
        {
            "review_id": row["review_id"],
            "voted_up": bool(row["voted_up"]),
            "review_text_preview": (row["review_text"] or "")[:CM_PLANNER_INSPECT_PREVIEW_CHARS],
            "primary_category": row["primary_category"],
            "timestamp": row["timestamp"],
        }
        for row in rows
    ]}


def _handle_fetch_game_reviews(input_payload: dict, ctx: PlannerContext) -> dict:
    """Wrap the existing fetch+clean+classify pipeline. Slow."""
    from pipeline.clean import clean_pipeline
    from pipeline.classify import run_classification
    from pipeline.ingest_reviews import fetch_all_reviews
    from pipeline.storage import get_connection, save_reviews

    app_id = input_payload["app_id"]
    if app_id in ctx.fetched_reviews_for:
        return {"fetched_count": 0, "classified_count": 0, "dropped_dup_count": 0,
                "error": f"already fetched reviews for {app_id} in this run; skipping"}

    max_reviews = max(20, min(CM_PLANNER_FETCH_REVIEWS_HARD_CAP,
                              int(input_payload.get("max_reviews", 50))))
    try:
        raw_reviews = fetch_all_reviews(app_id=app_id, max_reviews=max_reviews)
    except Exception as e:
        logger.exception("fetch_game_reviews: Steam API failed for %s", app_id)
        return {"fetched_count": 0, "classified_count": 0, "dropped_dup_count": 0,
                "error": f"Steam API unreachable: {type(e).__name__}: {e}"}

    if not raw_reviews:
        return {"fetched_count": 0, "classified_count": 0, "dropped_dup_count": 0,
                "error": "Steam API returned no reviews (invalid app_id?)"}

    fetched = len(raw_reviews)
    df = clean_pipeline(raw_reviews, app_id)
    cleaned = len(df)
    dropped_dup = fetched - cleaned

    conn = get_connection()
    try:
        save_reviews(conn, df)
        cls_summary = run_classification(conn, app_id, limit=cleaned)
    finally:
        conn.close()

    ctx.fetched_reviews_for.add(app_id)
    ctx.fetch_calls_made[app_id] = ctx.fetch_calls_made.get(app_id, 0) + 1
    return {
        "fetched_count": fetched,
        "classified_count": int(cls_summary.get("classified", 0)),
        "dropped_dup_count": dropped_dup,
        "error": None,
    }


def _handle_fetch_game_patch_notes(input_payload: dict, ctx: PlannerContext) -> dict:
    """Wrap fetch_news + chunk + embed. Idempotent (ChromaDB upsert)."""
    from pipeline.chunk import chunk_all_patch_notes
    from pipeline.ingest_patch_notes import fetch_news
    from pipeline.retrieve import embed_chunks

    app_id = input_payload["app_id"]
    if app_id in ctx.fetched_patch_notes_for:
        return {"fetched_count": 0, "indexed_chunk_count": 0,
                "error": f"already fetched patch notes for {app_id} in this run; skipping"}

    max_items = max(10, min(CM_PLANNER_FETCH_PATCH_NOTES_HARD_CAP,
                            int(input_payload.get("max_items", 50))))
    try:
        items = fetch_news(app_id=app_id, max_items=max_items)
    except Exception as e:
        logger.exception("fetch_game_patch_notes: Steam News API failed for %s", app_id)
        return {"fetched_count": 0, "indexed_chunk_count": 0,
                "error": f"Steam News API unreachable: {type(e).__name__}: {e}"}

    if not items:
        return {"fetched_count": 0, "indexed_chunk_count": 0,
                "error": "Steam News API returned no items"}

    chunk_result = chunk_all_patch_notes(items)
    embed_chunks(chunk_result, app_id)

    ctx.fetched_patch_notes_for.add(app_id)
    ctx.fetch_calls_made[app_id] = ctx.fetch_calls_made.get(app_id, 0) + 1
    return {
        "fetched_count": len(items),
        "indexed_chunk_count": len(chunk_result.children),
        "error": None,
    }


_INFO_TOOLS = {"count_matching_reviews", "inspect_reviews", "lookup_app_by_name"}
_FETCH_TOOLS = {"fetch_game_reviews", "fetch_game_patch_notes"}
_TERMINAL_TOOLS = {"draft_responses_for_batch", "reject_goal"}

_HANDLERS: dict[str, Callable[[dict, PlannerContext], dict]] = {
    "count_matching_reviews": _handle_count_matching_reviews,
    "inspect_reviews": _handle_inspect_reviews,
    "lookup_app_by_name": _handle_lookup_app_by_name,
    "fetch_game_reviews": _handle_fetch_game_reviews,
    "fetch_game_patch_notes": _handle_fetch_game_patch_notes,
}


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def _emit_planner_log(run_id: str, line: dict) -> None:
    """Append one JSONL line to evals/logs/cm_planner_<run_id>.jsonl. Best-effort."""
    if not run_id:
        return
    try:
        _PLANNER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (_PLANNER_LOG_DIR / f"cm_planner_{run_id}.jsonl").open("a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception as e:
        logger.warning(f"CM planner: JSONL log write failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Planner driver
# ---------------------------------------------------------------------------

class CMPlanner:
    """ReAct-style tool-use planner for CM batch goals."""

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)

    def plan(
        self,
        goal: str,
        current_date: str,
        run_id: str = "",
        event_emitter: Callable[[str, dict], None] | None = None,
    ) -> PlannerResult:
        """Drive the tool-use loop until a terminal action is selected.

        Args:
            goal: Free-text CM request.
            current_date: ISO date for relative-date resolution in the skill prompt.
            run_id: Used for the per-run JSONL log filename. Empty → no log.
            event_emitter: Optional callback called with (event_type, payload) on each
                tool dispatch and on terminal-action selection. cm_runner uses this to
                stream activity to the SSE generator without the planner knowing about SSE.

        Returns:
            PlannerResult — terminal_action plus the args needed by the orchestrator.
        """
        ctx = PlannerContext(event_emitter=event_emitter)
        user_message = (
            f"<goal>{escape_xml(goal)}</goal>\n"
            f"<current_date>{escape_xml(current_date)}</current_date>"
        )
        messages: list[dict] = [{"role": "user", "content": user_message}]
        tool_call_log: list[dict] = []

        for turn in range(CM_PLANNER_MAX_TOTAL_TURNS):
            response = self.client.messages.create(
                model=CM_PLANNER_MODEL,
                max_tokens=CM_PLANNER_MAX_TOKENS,
                temperature=CM_PLANNER_TEMPERATURE,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )

            if response.stop_reason == "refusal":
                return _force_reject(
                    "planner model refused the request",
                    ctx, tool_call_log, run_id,
                )

            if response.stop_reason in ("end_turn", "max_tokens"):
                # The LLM emitted text without picking a terminal tool — treat as
                # an implicit failure to commit. Force reject.
                if response.stop_reason == "max_tokens":
                    logger.warning(
                        "CM planner cut off at max_tokens on turn %d; forcing reject", turn,
                    )
                return _force_reject(
                    f"planner exited (stop_reason={response.stop_reason}) without picking a terminal action",
                    ctx, tool_call_log, run_id,
                )

            if response.stop_reason != "tool_use":
                return _force_reject(
                    f"planner unexpected stop_reason={response.stop_reason}",
                    ctx, tool_call_log, run_id,
                )

            # Append assistant turn (including tool_use blocks) to the conversation.
            messages.append({"role": "assistant", "content": response.content})

            tool_result_blocks: list[dict] = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                name = block.name  # type: ignore[union-attr]
                tool_input = block.input or {}  # type: ignore[union-attr]

                # ---- Terminal-tool short-circuit ----
                if name in _TERMINAL_TOOLS:
                    if name == "draft_responses_for_batch":
                        return _build_execute_result(
                            tool_input, ctx, tool_call_log, run_id,
                        )
                    elif name == "reject_goal":
                        return _build_reject_result(
                            tool_input, ctx, tool_call_log, run_id,
                        )

                # ---- Non-terminal: budget + dispatch ----
                if name not in _HANDLERS:
                    error_msg = f"unknown tool: {name}"
                    tool_result_blocks.append(_error_result(block.id, error_msg))  # type: ignore[union-attr]
                    continue

                # Budget gating
                if name in _INFO_TOOLS and ctx.info_calls_used >= CM_PLANNER_MAX_INFO_CALLS:
                    msg = (
                        "info-call budget exhausted — pick a terminal action now "
                        "(draft_responses_for_batch or reject_goal)"
                    )
                    tool_result_blocks.append(_error_result(block.id, msg))  # type: ignore[union-attr]
                    _log_call(tool_call_log, turn, name, tool_input, {"error": msg}, error=True)
                    if event_emitter:
                        event_emitter("cm_planner_tool_call", {
                            "turn": turn, "tool_name": name, "input": tool_input,
                        })
                        event_emitter("cm_planner_tool_result", {
                            "turn": turn, "tool_name": name, "output": {"error": msg}, "error": True,
                        })
                    continue
                if name in _FETCH_TOOLS and ctx.fetch_calls_used >= CM_PLANNER_MAX_FETCH_CALLS:
                    msg = (
                        "fetch-call budget exhausted — work with the data you have, "
                        "or reject_goal if it's insufficient"
                    )
                    tool_result_blocks.append(_error_result(block.id, msg))  # type: ignore[union-attr]
                    _log_call(tool_call_log, turn, name, tool_input, {"error": msg}, error=True)
                    if event_emitter:
                        event_emitter("cm_planner_tool_call", {
                            "turn": turn, "tool_name": name, "input": tool_input,
                        })
                        event_emitter("cm_planner_tool_result", {
                            "turn": turn, "tool_name": name, "output": {"error": msg}, "error": True,
                        })
                    continue

                # Emit start event before potentially long handler call.
                if event_emitter:
                    event_emitter("cm_planner_tool_call", {
                        "turn": turn, "tool_name": name, "input": tool_input,
                    })

                t0 = time.monotonic()
                try:
                    result = _HANDLERS[name](tool_input, ctx)
                    is_error = bool(result.get("error"))
                except Exception as e:  # noqa: BLE001 — surface any handler failure to the LLM
                    logger.exception("planner tool %s raised", name)
                    result = {"error": f"{type(e).__name__}: {e}"}
                    is_error = True
                latency_ms = int((time.monotonic() - t0) * 1000)

                if name in _INFO_TOOLS:
                    ctx.info_calls_used += 1
                if name in _FETCH_TOOLS and not is_error:
                    ctx.fetch_calls_used += 1

                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # type: ignore[union-attr]
                    "content": json.dumps(result),
                    "is_error": is_error,
                })
                _log_call(tool_call_log, turn, name, tool_input, result,
                          error=is_error, latency_ms=latency_ms)
                if event_emitter:
                    event_emitter("cm_planner_tool_result", {
                        "turn": turn, "tool_name": name, "output": result,
                        "error": is_error, "latency_ms": latency_ms,
                    })

            if not tool_result_blocks:
                # Haiku quirk — stop_reason=tool_use with no tool_use blocks. Force reject.
                logger.warning(
                    "[haiku-quirk:no-tool-blocks] CM planner stop_reason=tool_use with no "
                    "tool_use blocks (run_id=%s, turn=%d)", run_id, turn,
                )
                return _force_reject(
                    "planner emitted tool_use stop_reason without any tool_use blocks",
                    ctx, tool_call_log, run_id,
                )

            messages.append({"role": "user", "content": tool_result_blocks})

        # MAX_TOTAL_TURNS exhausted without a terminal action.
        logger.warning("CM planner exhausted MAX_TOTAL_TURNS=%d without committing",
                       CM_PLANNER_MAX_TOTAL_TURNS)
        if event_emitter:
            event_emitter("cm_planner_runaway", {
                "max_turns": CM_PLANNER_MAX_TOTAL_TURNS,
                "info_calls_used": ctx.info_calls_used,
                "fetch_calls_used": ctx.fetch_calls_used,
            })
        result = _force_reject(
            f"planner exceeded turn budget ({CM_PLANNER_MAX_TOTAL_TURNS}) without committing to an action",
            ctx, tool_call_log, run_id,
        )
        result.runaway = True
        return result


# ---------------------------------------------------------------------------
# Result-building helpers
# ---------------------------------------------------------------------------

def _build_execute_result(
    tool_input: dict, ctx: PlannerContext, log: list[dict], run_id: str,
) -> PlannerResult:
    """Validate + clamp draft_responses_for_batch args and build PlannerResult."""
    filt = dict(tool_input.get("filter") or {})
    # Clamp limit per the schema (defensive — Pydantic validation in upstream
    # API may already enforce, but the spike showed Haiku occasionally emits
    # limit=14 etc.).
    raw_limit = filt.get("limit", 5)
    try:
        clamped_limit = max(1, min(10, int(raw_limit)))
    except (TypeError, ValueError):
        clamped_limit = 5
    filt["limit"] = clamped_limit

    # Validate category if present (defensive — schema enum should catch but doesn't always).
    if filt.get("category") and filt["category"] not in REVIEW_CATEGORIES:
        return _force_reject(
            f"planner emitted unknown category: {filt['category']}",
            ctx, log, run_id,
        )

    result = PlannerResult(
        terminal_action="execute_batch",
        filter=filt,
        synthesis_instruction=str(tool_input.get("synthesis_instruction", "")).strip(),
        uncertain=bool(tool_input.get("uncertain", False)),
        concerns=list(tool_input.get("concerns") or []),
        fetch_calls_made=dict(ctx.fetch_calls_made),
        app_id_alternatives=list(ctx.app_id_alternatives),
        tool_call_log=log,
    )
    _emit_planner_log(run_id, {
        "event": "terminal", "terminal_action": "execute_batch",
        "filter": filt, "uncertain": result.uncertain, "concerns": result.concerns,
    })
    return result


def _build_reject_result(
    tool_input: dict, ctx: PlannerContext, log: list[dict], run_id: str,
) -> PlannerResult:
    """Validate reject_goal args and build PlannerResult."""
    reason = str(tool_input.get("reason", "")).strip()
    if len(reason) < 20:
        # Schema minLength=20 but Haiku sometimes emits shorter strings.
        reason = (reason + " (planner emitted insufficient justification)").strip()
    result = PlannerResult(
        terminal_action="reject", reason=reason,
        fetch_calls_made=dict(ctx.fetch_calls_made),
        tool_call_log=log,
    )
    _emit_planner_log(run_id, {
        "event": "terminal", "terminal_action": "reject", "reason": reason,
    })
    return result


def _force_reject(
    reason: str, ctx: PlannerContext, log: list[dict], run_id: str,
) -> PlannerResult:
    """Internal — used when the loop hits an unrecoverable state."""
    result = PlannerResult(
        terminal_action="reject", reason=reason,
        fetch_calls_made=dict(ctx.fetch_calls_made),
        tool_call_log=log,
    )
    _emit_planner_log(run_id, {
        "event": "force_reject", "reason": reason,
    })
    return result


def _error_result(tool_use_id: str, message: str) -> dict:
    """Tool-result block tagged is_error so the LLM can recover or escalate."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": message,
        "is_error": True,
    }


def _log_call(
    log: list[dict], turn: int, name: str, input_payload: dict, output: dict,
    error: bool = False, latency_ms: int | None = None,
) -> None:
    """Append a tool-call entry for both in-memory tool_call_log and JSONL persistence."""
    entry = {
        "turn": turn,
        "tool_name": name,
        "input": input_payload,
        "output": output,
        "error": error,
        "latency_ms": latency_ms,
    }
    log.append(entry)
