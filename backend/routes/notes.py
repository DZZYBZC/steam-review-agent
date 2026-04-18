"""
Memory tab — exposes the agent's actual operational memory.

Reads the PROMOTED subset of cluster_notes (what the investigator sees at
runtime) and audit_log (the responder's few-shot pool). Filters out is_demo
writes so interview-demo runs never appear here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import DB_PATH
from backend.services.games import game_name
from pipeline.storage import (
    get_connection,
    promote_audit_entry,
    promote_cluster_note,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/memory/summary")
def memory_summary() -> dict[str, Any]:
    """Return promoted memory surfaces: cluster notes (investigator context)
    and audit-log entries (responder few-shot pool), filtered to exclude demo
    writes. Both are grouped by category + game for rendering."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # Promoted cluster notes — what the investigator reads
        note_rows = conn.execute(
            """
            SELECT id, app_id, category, note_type, note_text, source_review_id,
                   promoted_by, promoted_at, created_at, is_eval, is_demo
            FROM cluster_notes
            WHERE promoted_at IS NOT NULL
              AND COALESCE(is_demo, 0) = 0
            ORDER BY category, note_type, promoted_at DESC
            """
        ).fetchall()
        notes = []
        for r in note_rows:
            r = dict(r)
            r["game_name"] = game_name(r["app_id"])
            notes.append(r)

        # Promoted audit entries — what the responder reads as few-shot
        audit_rows = conn.execute(
            """
            SELECT run_id, app_id, category, review_id, review_text,
                   evidence_summary, evidence_confidence, drafted_response,
                   proposed_action, critique, iteration_count,
                   promoted_by, promoted_at, created_at, is_eval, is_demo
            FROM audit_log
            WHERE promoted_at IS NOT NULL
              AND COALESCE(is_demo, 0) = 0
            ORDER BY category, promoted_at DESC
            """
        ).fetchall()
        audits = []
        for r in audit_rows:
            r = dict(r)
            r["game_name"] = game_name(r["app_id"])
            # Truncate long fields for the list; full text available via /memory/audit/{run_id}
            rt = r.pop("review_text", "") or ""
            r["review_preview"] = rt[:160] + ("…" if len(rt) > 160 else "")
            dr = r.pop("drafted_response", "") or ""
            r["drafted_preview"] = dr[:200] + ("…" if len(dr) > 200 else "")
            audits.append(r)

        # Raw-pool totals for the "isolation architecture" badge
        total_notes_all = conn.execute(
            "SELECT COUNT(*) FROM cluster_notes"
        ).fetchone()[0]
        total_notes_promoted = conn.execute(
            "SELECT COUNT(*) FROM cluster_notes WHERE promoted_at IS NOT NULL"
        ).fetchone()[0]
        total_notes_is_eval = conn.execute(
            "SELECT COUNT(*) FROM cluster_notes WHERE COALESCE(is_eval,0)=1"
        ).fetchone()[0]
        total_notes_is_demo = conn.execute(
            "SELECT COUNT(*) FROM cluster_notes WHERE COALESCE(is_demo,0)=1"
        ).fetchone()[0]

        total_audit_all = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        total_audit_promoted = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE promoted_at IS NOT NULL"
        ).fetchone()[0]

    # Per-category counts
    def _bucket(rows, keys):
        buckets: dict[tuple, int] = {}
        for r in rows:
            k = tuple(r.get(key) for key in keys)
            buckets[k] = buckets.get(k, 0) + 1
        return [{**dict(zip(keys, k)), "count": v} for k, v in sorted(buckets.items())]

    return {
        "totals": {
            "cluster_notes": {
                "all": total_notes_all,
                "promoted_visible": len(notes),
                "promoted_total": total_notes_promoted,
                "eval_tagged": total_notes_is_eval,
                "demo_tagged": total_notes_is_demo,
            },
            "audit_log": {
                "all": total_audit_all,
                "promoted_visible": len(audits),
                "promoted_total": total_audit_promoted,
            },
        },
        "notes_by_type": _bucket(notes, ["note_type"]),
        "notes_by_category": _bucket(notes, ["category", "note_type"]),
        "notes_by_game": _bucket(notes, ["game_name"]),
        "notes": notes,
        "audits": audits,
    }


class PromoteRunRequest(BaseModel):
    run_id: str
    promote_audit: bool = False  # explicit opt-in per item
    promote_note_ids: list[int] = []


@router.get("/memory/pending/{run_id}")
def pending_promotion_detail(run_id: str) -> dict[str, Any]:
    """Full content of the audit entry + related cluster notes that would be
    promoted. Lets the UI render each item for per-item user review before
    they decide what to keep.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        audit = conn.execute(
            """
            SELECT run_id, app_id, review_id, category, review_text,
                   evidence_summary, drafted_response, proposed_action,
                   critique, iteration_count, stop_reason, created_at,
                   is_demo, promoted_at
            FROM audit_log WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if audit is None:
            raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
        audit = dict(audit)
        notes = [dict(r) for r in conn.execute(
            """
            SELECT id, app_id, category, note_type, note_text, source_review_id,
                   created_by, created_at, is_demo, promoted_at
            FROM cluster_notes
            WHERE source_review_id = ?
              AND COALESCE(is_demo, 0) = 1
              AND datetime(created_at) >= datetime(?, '-2 minutes')
              AND datetime(created_at) <= datetime(?, '+10 minutes')
            ORDER BY created_at ASC
            """,
            (audit["review_id"], audit["created_at"], audit["created_at"]),
        ).fetchall()]
    audit["game_name"] = game_name(audit.get("app_id"))
    for n in notes:
        n["game_name"] = game_name(n.get("app_id"))
    return {"run_id": run_id, "audit": audit, "notes": notes}


@router.post("/memory/promote")
def promote_run(req: PromoteRunRequest) -> dict[str, Any]:
    """Promote selected items from a demo-tagged run into production memory.

    Client opts in per-item via `promote_audit` (bool) and `promote_note_ids`
    (list of cluster_note ids). Only the chosen rows are un-tagged from demo
    and promoted; unspecified items stay demo-tagged (invisible to prod reads).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT run_id, app_id, category, review_id
            FROM audit_log WHERE run_id = ?
            """,
            (req.run_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run_id {req.run_id!r} not found")
        run_id, app_id, category, review_id = row

        n_audit = 0
        if req.promote_audit:
            n_audit = conn.execute(
                "UPDATE audit_log SET is_demo = 0 WHERE run_id = ?",
                (req.run_id,),
            ).rowcount
            conn.commit()
            promote_audit_entry(conn, run_id=req.run_id, promoted_by="manual_demo_promote")

        note_ids = list(req.promote_note_ids or [])
        if note_ids:
            placeholders = ",".join("?" * len(note_ids))
            conn.execute(
                f"UPDATE cluster_notes SET is_demo = 0 WHERE id IN ({placeholders})",
                note_ids,
            )
            conn.commit()
            for nid in note_ids:
                promote_cluster_note(conn, note_id=nid, promoted_by="manual_demo_promote")
    finally:
        conn.close()

    return {
        "ok": True,
        "run_id": req.run_id,
        "app_id": app_id,
        "review_id": review_id,
        "category": category,
        "audit_promoted": n_audit,
        "notes_promoted": len(note_ids),
    }
