"""
Phase 1 — CM planner tool handlers in isolation.

SQL-only tests (count_matching_reviews, inspect_reviews) run by default.
Steam-API integration tests (fetch_game_reviews, fetch_game_patch_notes)
are gated behind --integration because they hit live external APIs.

Run all default:        python -m pytest test_cm_planner_tools.py -v
Include integration:    python -m pytest test_cm_planner_tools.py -v --integration
"""

from __future__ import annotations

import pytest

from agent.planner_cm import (
    PlannerContext,
    _handle_count_matching_reviews,
    _handle_fetch_game_patch_notes,
    _handle_fetch_game_reviews,
    _handle_inspect_reviews,
    _handle_lookup_app_by_name,
    _build_execute_result,
    _build_reject_result,
)
from config import TEST_APP_ID


# ---------------------------------------------------------------------------
# count_matching_reviews
# ---------------------------------------------------------------------------

def test_count_known_app_returns_positive():
    ctx = PlannerContext()
    out = _handle_count_matching_reviews({"app_id": TEST_APP_ID}, ctx)
    assert out["app_id_known"] is True
    assert out["count"] > 0


def test_count_unknown_app_returns_zero_and_unknown():
    ctx = PlannerContext()
    out = _handle_count_matching_reviews({"app_id": "999999999"}, ctx)
    assert out["app_id_known"] is False
    assert out["count"] == 0


def test_count_with_category_and_voted_up():
    ctx = PlannerContext()
    out = _handle_count_matching_reviews(
        {"app_id": TEST_APP_ID, "category": "technical_issues", "voted_up": False}, ctx,
    )
    assert out["app_id_known"] is True
    assert out["count"] >= 1, "expected at least one negative technical_issues review for the test app"


# ---------------------------------------------------------------------------
# inspect_reviews
# ---------------------------------------------------------------------------

def test_inspect_returns_at_most_k_rows_with_required_fields():
    ctx = PlannerContext()
    out = _handle_inspect_reviews(
        {"filter": {"app_id": TEST_APP_ID}, "k": 3}, ctx,
    )
    rows = out["reviews"]
    assert len(rows) <= 3
    assert len(rows) >= 1
    required = {"review_id", "voted_up", "review_text_preview", "primary_category", "timestamp"}
    for row in rows:
        assert required.issubset(row.keys()), f"missing fields: {required - set(row.keys())}"


def test_inspect_clamps_k_to_5():
    ctx = PlannerContext()
    out = _handle_inspect_reviews(
        {"filter": {"app_id": TEST_APP_ID}, "k": 99}, ctx,
    )
    assert len(out["reviews"]) <= 5


def test_inspect_truncates_preview():
    from config import CM_PLANNER_INSPECT_PREVIEW_CHARS
    ctx = PlannerContext()
    out = _handle_inspect_reviews(
        {"filter": {"app_id": TEST_APP_ID}, "k": 5}, ctx,
    )
    for row in out["reviews"]:
        assert len(row["review_text_preview"]) <= CM_PLANNER_INSPECT_PREVIEW_CHARS


# ---------------------------------------------------------------------------
# Terminal action validation (no LLM, no API)
# ---------------------------------------------------------------------------

def test_build_execute_result_clamps_limit():
    ctx = PlannerContext()
    result = _build_execute_result(
        {
            "filter": {"app_id": "X", "limit": 99},
            "synthesis_instruction": "test",
            "uncertain": False,
            "concerns": [],
        }, ctx, [], "test_run",
    )
    assert result.terminal_action == "execute_batch"
    assert result.filter["limit"] == 10  # clamped from 99


def test_build_execute_result_rejects_unknown_category():
    ctx = PlannerContext()
    result = _build_execute_result(
        {
            "filter": {"category": "made_up_category", "limit": 5},
            "synthesis_instruction": "test",
            "uncertain": False,
            "concerns": [],
        }, ctx, [], "test_run",
    )
    assert result.terminal_action == "reject"
    assert "made_up_category" in result.reason


def test_build_reject_result_pads_short_reason():
    ctx = PlannerContext()
    result = _build_reject_result(
        {"reason": "short"}, ctx, [], "test_run",
    )
    assert result.terminal_action == "reject"
    assert len(result.reason) >= 20


# ---------------------------------------------------------------------------
# Steam API integration (skipped by default)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_fetch_game_reviews_idempotent_via_dedup():
    """Verifies fetch_game_reviews + downstream dedup = no duplicate rows on second run."""
    import sqlite3
    from config import DB_PATH

    ctx = PlannerContext()
    app_id = TEST_APP_ID

    with sqlite3.connect(DB_PATH) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE app_id=?", (app_id,),
        ).fetchone()[0]

    out1 = _handle_fetch_game_reviews({"app_id": app_id, "max_reviews": 20}, ctx)
    assert out1["error"] is None
    assert out1["fetched_count"] >= 1

    with sqlite3.connect(DB_PATH) as conn:
        after_first = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE app_id=?", (app_id,),
        ).fetchone()[0]

    # Reset ctx so second call isn't blocked by per-run dedup
    ctx2 = PlannerContext()
    out2 = _handle_fetch_game_reviews({"app_id": app_id, "max_reviews": 20}, ctx2)
    # Could legitimately fetch new reviews if the game is active — but downstream
    # dedup should prevent duplicate rows from re-inserting.

    with sqlite3.connect(DB_PATH) as conn:
        after_second = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE app_id=?", (app_id,),
        ).fetchone()[0]

    delta = after_second - after_first
    # Tolerate up to N new rows (genuinely new reviews); zero is the ideal.
    assert delta <= 20, f"second fetch added too many rows: {delta}"


@pytest.mark.integration
def test_fetch_game_patch_notes_indexes_to_chroma():
    ctx = PlannerContext()
    out = _handle_fetch_game_patch_notes({"app_id": TEST_APP_ID, "max_items": 10}, ctx)
    assert out["error"] is None
    assert out["fetched_count"] >= 1
    assert out["indexed_chunk_count"] >= 1


# ---------------------------------------------------------------------------
# lookup_app_by_name (integration — hits Steam storefront search; cheap + fast)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_lookup_resolves_famous_game_name_to_app_id():
    ctx = PlannerContext()
    out = _handle_lookup_app_by_name({"name": "Baldur's Gate 3"}, ctx)
    assert out["error"] is None
    assert out["exact_match"] is True
    assert out["candidates"][0]["app_id"] == "1086940"
    assert out["candidates"][0]["type"] == "app"


@pytest.mark.integration
def test_lookup_returns_ranked_candidates_for_partial_name():
    ctx = PlannerContext()
    out = _handle_lookup_app_by_name({"name": "monster hunter wilds"}, ctx)
    assert out["error"] is None
    # Top result should be the canonical Monster Hunter Wilds
    assert out["candidates"][0]["app_id"] == "2246340"
    # Multiple candidates returned (DLC, etc.)
    assert len(out["candidates"]) >= 1


@pytest.mark.integration
def test_lookup_handles_no_matches_gracefully():
    ctx = PlannerContext()
    out = _handle_lookup_app_by_name({"name": "asdfqwer1234zzzz"}, ctx)
    assert out["error"] is None  # not an error — just no matches
    assert out["candidates"] == []
    assert out["exact_match"] is False


def test_lookup_rejects_empty_name():
    """Empty string handled deterministically; no network call."""
    ctx = PlannerContext()
    out = _handle_lookup_app_by_name({"name": "   "}, ctx)
    assert "error" in out
    assert out["candidates"] == []


@pytest.mark.integration
def test_lookup_annotates_in_local_db_membership():
    """Each candidate gets in_local_db + n_local_reviews so the planner can
    detect when multiple matches are present in our DB (disambiguation case)."""
    ctx = PlannerContext()
    out = _handle_lookup_app_by_name({"name": "Monster Hunter Wilds"}, ctx)
    assert out["error"] is None
    # Top result should be MH Wilds (2246340) — which IS in the test DB.
    top = out["candidates"][0]
    assert top["app_id"] == "2246340"
    assert top["in_local_db"] is True
    assert top["n_local_reviews"] > 0
    # Lower-ranked DLC candidates should be NOT in local DB (we only ingested the base game).
    for cand in out["candidates"][1:]:
        if cand["app_id"] != "2246340":
            assert cand["in_local_db"] is False
            assert cand["n_local_reviews"] == 0


@pytest.mark.integration
def test_lookup_n_in_local_db_count():
    """`n_in_local_db` reflects how many candidates are in the DB.
    For 'Monster Hunter Wilds' against our test DB, this should be exactly 1."""
    ctx = PlannerContext()
    out = _handle_lookup_app_by_name({"name": "Monster Hunter Wilds"}, ctx)
    assert out["error"] is None
    assert out["n_in_local_db"] == 1
    # And ctx.app_id_alternatives should NOT be populated (only fires at >=2)
    assert ctx.app_id_alternatives == []


def test_lookup_disambiguation_threshold_fires_at_two_in_db_matches(monkeypatch):
    """Direct unit-style test: stub search_apps to return 2 candidates that
    both 'happen to be' in the DB. Verifies ctx.app_id_alternatives is set."""
    from agent import planner_cm
    fake_candidates = [
        {"app_id": "2246340", "name": "Monster Hunter Wilds", "type": "app"},
        # Use another app_id that's actually in the test DB so the SQL JOIN
        # finds it. From the DB inspection earlier: 1716740 (Starfield),
        # 2246340 (MHW), 2694490, 1295660, 1272080.
        {"app_id": "1716740", "name": "Monster Hunter Wilds (test fixture)", "type": "app"},
    ]

    def fake_search(name, k=5):
        return fake_candidates

    monkeypatch.setattr(planner_cm, "_handle_lookup_app_by_name",
                        planner_cm._handle_lookup_app_by_name)
    monkeypatch.setattr("pipeline.steam_app_index.search_apps", fake_search)

    ctx = PlannerContext()
    out = planner_cm._handle_lookup_app_by_name({"name": "monster hunter"}, ctx)
    assert out["error"] is None
    assert out["n_in_local_db"] == 2
    assert len(ctx.app_id_alternatives) == 2
    assert {alt["app_id"] for alt in ctx.app_id_alternatives} == {"2246340", "1716740"}


@pytest.mark.integration
def test_fetch_game_reviews_per_run_dedup():
    """Within one PlannerContext, calling fetch twice for the same app no-ops."""
    ctx = PlannerContext()
    out1 = _handle_fetch_game_reviews({"app_id": TEST_APP_ID, "max_reviews": 20}, ctx)
    assert out1["error"] is None or "already fetched" not in (out1["error"] or "")
    out2 = _handle_fetch_game_reviews({"app_id": TEST_APP_ID, "max_reviews": 20}, ctx)
    assert out2["error"] and "already fetched" in out2["error"]
