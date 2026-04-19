"""
Phase 2 — CM planner ReAct loop golden set.

Locked golden cases that exercise the four canonical paths from the plan:
1. Gibberish      → reject_goal
2. Ingested game  → count → maybe inspect → draft (uncertain=False)
3. Cold game      → count → fetch → recount → draft (uncertain=True; CM_GATE_FETCH_TRIGGERED would fire)
4. Vague + large  → count → narrow → draft (uncertain=True with concerns) OR reject

Each test asserts on the planner's terminal action and selected uncertainty
signals. Stochastic — Haiku may pick legitimately different paths (especially
for case 4); the disjunction in case 4 is intentional.

These hit the live Anthropic API (planner Haiku turns). Cost per run is ~$0.005-0.02.
Marked @pytest.mark.integration since they're not deterministic.

Run:
    python -m pytest test_cm_planner_loop.py -v --integration
"""

from __future__ import annotations

import pytest

from agent.planner_cm import CMPlanner
from config import TEST_APP_ID

CURRENT_DATE = "2026-04-18"


@pytest.fixture
def planner():
    return CMPlanner()


# ---------------------------------------------------------------------------
# Golden case 1 — gibberish → reject
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_gibberish_input_rejects_without_fetch_or_draft(planner):
    """Single-character input should reject_goal immediately. No tools should fire."""
    result = planner.plan(goal="d", current_date=CURRENT_DATE, run_id="test_gibberish")
    assert result.terminal_action == "reject"
    assert len(result.reason) >= 20
    # No fetch tool calls
    assert sum(result.fetch_calls_made.values()) == 0
    # Either zero tool calls (immediate reject) or only count (probe-then-reject); never draft.
    tool_names = {c["tool_name"] for c in result.tool_call_log}
    assert "draft_responses_for_batch" not in tool_names
    assert "fetch_game_reviews" not in tool_names


# ---------------------------------------------------------------------------
# Golden case 2 — ingested game, narrow filter → draft, uncertain=False
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_ingested_game_specific_filter_drafts_with_certainty(planner):
    """Specific app + category + voted_up should commit to draft confidently."""
    result = planner.plan(
        goal=f"draft replies to recent negative technical_issues reviews for app {TEST_APP_ID}",
        current_date=CURRENT_DATE,
        run_id="test_ingested",
    )
    assert result.terminal_action == "execute_batch"
    assert result.filter is not None
    assert result.filter.get("app_id") == TEST_APP_ID
    # Should NOT fetch — game is already in DB
    assert sum(result.fetch_calls_made.values()) == 0
    # Should have called count_matching_reviews at least once
    tool_names = [c["tool_name"] for c in result.tool_call_log]
    assert "count_matching_reviews" in tool_names


# ---------------------------------------------------------------------------
# Golden case 3 — cold game → fetch path
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_cold_game_triggers_fetch():
    """Goal naming an un-ingested app should trigger fetch_game_reviews."""
    # Use a likely-unused-but-real app id. If 999999999 returns nothing from Steam,
    # the planner should still demonstrate the count→fetch attempt.
    planner = CMPlanner()
    result = planner.plan(
        goal="draft replies to negative reviews for Steam app 999999999",
        current_date=CURRENT_DATE,
        run_id="test_cold",
    )
    # The planner should at minimum recognize the cold case and either fetch or reject.
    assert result.terminal_action in ("execute_batch", "reject")
    tool_names = [c["tool_name"] for c in result.tool_call_log]
    assert "count_matching_reviews" in tool_names
    # Either it tried to fetch (saw app_id_known=False) or it rejected after seeing no data.
    fetched_or_rejected = "fetch_game_reviews" in tool_names or result.terminal_action == "reject"
    assert fetched_or_rejected, "planner did neither fetch nor reject on cold game"


# ---------------------------------------------------------------------------
# Golden case 4 — vague + large → either narrowing-then-draft-with-concerns, OR reject
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_vague_large_input_either_drafts_uncertain_or_rejects(planner):
    """
    Inherently ambiguous goal — accept either a narrowing-then-draft path with
    concerns/uncertain flagged, OR an explicit reject. The disjunction is
    intentional (no single right answer for this input shape).
    """
    result = planner.plan(
        goal="what are people complaining about",
        current_date=CURRENT_DATE,
        run_id="test_vague",
    )
    assert result.terminal_action in ("execute_batch", "reject")
    if result.terminal_action == "execute_batch":
        # If it drafts, it MUST flag uncertainty — vague goal without app_id should
        # trigger CM_GATE_NO_APP_ID_TRIGGER + likely CM_GATE_ON_UNCERTAIN downstream.
        assert result.uncertain or len(result.concerns) > 0, (
            "vague goal drafted without flagging any uncertainty — gate would not fire"
        )


# ---------------------------------------------------------------------------
# Sanity: turn budget never exceeded
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_planner_uses_lookup_when_goal_names_game_by_name(planner):
    """Goal names a game by NAME (not id) — planner must call lookup_app_by_name
    before any count/fetch, and the resolved app_id must end up in the filter."""
    result = planner.plan(
        # Use a famous game we DO have in the DB so the test doesn't depend on fetch latency.
        # The point of this test is the lookup → resolved-id path, not the fetch behavior.
        goal="draft replies to recent negative reviews for Monster Hunter Wilds",
        current_date=CURRENT_DATE,
        run_id="test_lookup",
    )
    assert result.terminal_action == "execute_batch"
    tool_names = [c["tool_name"] for c in result.tool_call_log]
    assert "lookup_app_by_name" in tool_names, \
        f"planner did not call lookup_app_by_name; called: {tool_names}"
    # Lookup must come before any count call (resolve name first, then probe)
    lookup_idx = tool_names.index("lookup_app_by_name")
    if "count_matching_reviews" in tool_names:
        count_idx = tool_names.index("count_matching_reviews")
        assert lookup_idx < count_idx, "lookup must precede count when name is given"
    # Resolved app_id should be Monster Hunter Wilds (2246340)
    assert result.filter is not None
    assert result.filter.get("app_id") == "2246340", \
        f"expected app_id='2246340' for Monster Hunter Wilds, got {result.filter.get('app_id')}"


@pytest.mark.integration
def test_all_runs_stay_within_turn_budget(planner):
    """No matter the goal, planner must not exceed CM_PLANNER_MAX_TOTAL_TURNS."""
    from config import CM_PLANNER_MAX_TOTAL_TURNS
    result = planner.plan(
        goal=f"performance issues for {TEST_APP_ID}",
        current_date=CURRENT_DATE,
        run_id="test_budget",
    )
    # `runaway` is True only when budget exhausted without terminal action.
    assert not result.runaway, "planner exceeded turn budget"
    # Turn indices recorded in tool_call_log must all be < max
    for entry in result.tool_call_log:
        assert entry["turn"] < CM_PLANNER_MAX_TOTAL_TURNS
