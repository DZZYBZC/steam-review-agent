"""
Review candidates for the Try Live sidebar.

Surfaces a diverse, deterministically-ordered sample of unprocessed reviews
(no audit_log entry yet) that have a classifier output. The interviewer can
pick any of them to drive a live agent run.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Query

from config import DB_PATH
from backend.services.games import game_name

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reviews/candidates")
def list_candidates(limit: int = Query(24, ge=1, le=100)) -> dict[str, Any]:
    """Return up to `limit` unprocessed reviews with classification, mixed
    across categories so the list feels diverse. Deterministic — no random
    ordering — so interviewers see the same list on every load.
    """
    # Pull a wide pool, then round-robin by category for diversity.
    # Note: we intentionally do NOT exclude reviews that have an audit_log entry
    # — the live run is still a fresh agent invocation with a new run_id, and
    # processed reviews are known-interesting (real audit data exists for them).
    pool_size = max(limit * 6, 120)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT r.review_id, r.app_id, r.review_text, r.voted_up, r.playtime_hours,
                   c.primary_category
            FROM reviews r
            INNER JOIN classifications c ON r.review_id = c.review_id
            WHERE r.is_near_duplicate = 0
              AND LENGTH(r.review_text) >= 40
              AND c.primary_category IS NOT NULL
              AND c.primary_category != 'other'
            ORDER BY r.review_id ASC
            LIMIT ?
            """,
            (pool_size,),
        ).fetchall()

    # RECOMMENDED_LIVE_PICKS intentionally NOT surfaced here — the demo story
    # for Try Live is "any interviewer-picked review runs through the real
    # agent." Featured cases (replay tab) cover the curated scripted paths;
    # live keeps the feel of genuine unprocessed reviews.

    if not rows:
        return {"candidates": [], "note": "no unprocessed classified reviews available"}

    # Bucket by (app_id, category) for diversity; round-robin across buckets.
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        r = dict(row)
        key = (r["app_id"], r["primary_category"])
        buckets.setdefault(key, []).append(r)

    keys = sorted(buckets.keys())
    picked: list[dict[str, Any]] = []
    while len(picked) < limit:
        before = len(picked)
        for key in keys:
            if len(picked) >= limit:
                break
            lst = buckets[key]
            if lst:
                picked.append(lst.pop(0))
        if len(picked) == before:
            break

    candidates = []
    for r in picked:
        text = r["review_text"] or ""
        flat = " ".join(text.split())
        preview = flat[:177] + "…" if len(flat) > 180 else flat
        candidates.append({
            "review_id": r["review_id"],
            "app_id": r["app_id"],
            "game_name": game_name(r["app_id"]),
            "category": r["primary_category"],
            "voted_up": bool(r["voted_up"]),
            "playtime_hours": r.get("playtime_hours"),
            "preview": preview,
            "review_text": text,
        })
    return {"candidates": candidates}
