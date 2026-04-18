"""
One-off curation script for featured demo cases.

Runs four targeted queries over audit_log + audit_log_iterations — one per
demo-path label (clean approval / rejection-revision / action override /
edge-insufficient-evidence) — and prints the top few candidates for each
with enough context to eyeball-pick a winner.

After picking, paste the chosen run_ids into backend/services/featured.py
along with hand-written talk_tracks. This script is intentionally one-shot;
it is not wired into the app.

Usage:
    python -m backend.scripts.pick_featured [--limit 5]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import textwrap
from typing import Any

from config import DB_PATH


def _preview(text: str | None, n: int = 140) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def pick_clean_approvals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """iteration_count == 0 means the critic approved the first draft."""
    return _fetch(
        conn,
        """
        SELECT al.run_id, al.app_id, al.category, al.review_id,
               al.proposed_action, al.iteration_count, al.review_text
        FROM audit_log al
        WHERE al.stop_reason = 'human_approved'
          AND al.human_decision = 'approved'
          AND al.iteration_count = 0
          AND al.run_id IS NOT NULL
        ORDER BY al.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def pick_rejection_revisions(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """First iter rejected with drafting reason; final approved."""
    return _fetch(
        conn,
        """
        SELECT al.run_id, al.app_id, al.category, al.review_id,
               al.proposed_action, al.iteration_count, al.review_text
        FROM audit_log al
        WHERE al.stop_reason = 'human_approved'
          AND al.iteration_count >= 1
          AND al.run_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM audit_log_iterations it
              WHERE it.run_id = al.run_id
                AND it.iteration = 0
                AND it.approved = 0
                AND it.reason_type = 'drafting'
          )
        ORDER BY al.iteration_count DESC, al.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def pick_action_overrides(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Action-freeze path: last iter rejected with reason_type='action' but run ended human_approved."""
    return _fetch(
        conn,
        """
        SELECT al.run_id, al.app_id, al.category, al.review_id,
               al.proposed_action, al.iteration_count, al.review_text,
               (SELECT it.proposed_action
                FROM audit_log_iterations it
                WHERE it.run_id = al.run_id
                ORDER BY it.iteration DESC
                LIMIT 1) AS last_iter_action,
               (SELECT it.reason_type
                FROM audit_log_iterations it
                WHERE it.run_id = al.run_id
                ORDER BY it.iteration DESC
                LIMIT 1) AS last_iter_reason
        FROM audit_log al
        WHERE al.stop_reason = 'human_approved'
          AND al.run_id IS NOT NULL
          AND (
              SELECT it.reason_type
              FROM audit_log_iterations it
              WHERE it.run_id = al.run_id
              ORDER BY it.iteration DESC
              LIMIT 1
          ) = 'action'
          AND (
              SELECT it.approved
              FROM audit_log_iterations it
              WHERE it.run_id = al.run_id
              ORDER BY it.iteration DESC
              LIMIT 1
          ) = 0
        ORDER BY al.iteration_count DESC, al.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def pick_edge_cases(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Proxy for 'insufficient evidence': final action is monitor/no_action AND evidence_summary
    mentions uncertainty, OR evidence_confidence is low. Since known_unknowns isn't persisted,
    this is a heuristic."""
    return _fetch(
        conn,
        """
        SELECT al.run_id, al.app_id, al.category, al.review_id,
               al.proposed_action, al.iteration_count,
               al.evidence_confidence, al.evidence_summary, al.review_text
        FROM audit_log al
        WHERE al.stop_reason = 'human_approved'
          AND al.human_decision = 'approved'
          AND al.proposed_action IN ('monitor', 'no_action')
          AND al.run_id IS NOT NULL
          AND (
              al.evidence_confidence <= 0.5
              OR LOWER(COALESCE(al.evidence_summary, '')) LIKE '%unclear%'
              OR LOWER(COALESCE(al.evidence_summary, '')) LIKE '%unknown%'
              OR LOWER(COALESCE(al.evidence_summary, '')) LIKE '%no direct%'
              OR LOWER(COALESCE(al.evidence_summary, '')) LIKE '%no patch%'
          )
        ORDER BY al.evidence_confidence ASC, al.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def _print_section(title: str, candidates: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 70}\n{title}  ({len(candidates)} candidates)\n{'=' * 70}")
    if not candidates:
        print("  (no candidates found)")
        return
    for c in candidates:
        print(f"\n  run_id:     {c['run_id']}")
        print(f"  app_id:     {c['app_id']}   category: {c['category']}")
        print(f"  iters:      {c.get('iteration_count')}   action: {c.get('proposed_action')}")
        if "last_iter_action" in c:
            print(f"  last iter:  action={c['last_iter_action']}  reason={c['last_iter_reason']}")
        if "evidence_confidence" in c and c.get("evidence_confidence") is not None:
            print(f"  ev. conf:   {c['evidence_confidence']:.2f}")
        print(f"  review:     {textwrap.shorten(_preview(c.get('review_text')), width=140)}")
        if c.get("evidence_summary"):
            print(f"  ev. summ:   {textwrap.shorten(_preview(c['evidence_summary']), width=140)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pick featured demo cases from audit_log.")
    parser.add_argument("--limit", type=int, default=5, help="Top N candidates per bucket")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of prose")
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        buckets = {
            "clean_approval": pick_clean_approvals(conn, args.limit),
            "rejection_revision": pick_rejection_revisions(conn, args.limit),
            "action_override": pick_action_overrides(conn, args.limit),
            "edge_insufficient_evidence": pick_edge_cases(conn, args.limit),
        }

    if args.json:
        print(json.dumps(buckets, indent=2, default=str))
        return

    _print_section("1. Clean approval (iter_count=0)", buckets["clean_approval"])
    _print_section("2. Rejection → revision (iter 0 drafting, final approved)", buckets["rejection_revision"])
    _print_section("3. Action override / freeze (last iter reason_type=action)", buckets["action_override"])
    _print_section("4. Edge / insufficient evidence (low confidence OR uncertainty in summary)", buckets["edge_insufficient_evidence"])


if __name__ == "__main__":
    main()
