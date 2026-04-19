"""
test_cm.py — End-to-end smoke test for the CM (Community Manager) batch runner.

Drains backend.services.cm_runner.run_cm synchronously with a hardcoded goal
and asserts the canonical event sequence. All sub-runs are tagged is_demo=True
by live_runner.build_initial_state, so this test does not pollute the promoted
audit / cluster-note pool.

Run it with: python test_cm.py
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import Counter

from backend.services.cm_runner import resolve_pending_confirm, run_cm
from config import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

GOAL = "urgent negative technical_issues reviews for app 2246340, limit 3"


def _short_payload(event: dict) -> dict:
    """Return a compact payload view for printing — trims long fields."""
    payload = dict(event.get("payload") or {})
    for key in ("candidates", "plan", "markdown", "current_draft", "drafted_response_preview"):
        if key in payload:
            val = payload[key]
            if isinstance(val, str) and len(val) > 120:
                payload[key] = val[:120] + "…"
            elif isinstance(val, list):
                payload[key] = f"<list of {len(val)} items>"
            elif isinstance(val, dict):
                payload[key] = f"<dict keys={list(val.keys())}>"
    return payload


def test_cm():
    meta_run_id = str(uuid.uuid4())
    logger.info("=== Running CM batch ===")
    logger.info(f"goal: {GOAL}")
    logger.info(f"meta_run_id: {meta_run_id}")

    # Universal gate (CM_GATE_ALWAYS=True) parks the orchestrator at every run.
    # Auto-approve from a daemon thread so the synchronous run_cm() drain
    # below doesn't hang. Polls because the gate can fire at any point after
    # the planner exits — the time depends on planner turn count + LLM latency.
    def _auto_approve_when_gated():
        deadline = time.monotonic() + 120  # generous: planner + count + gate
        while time.monotonic() < deadline:
            if resolve_pending_confirm(meta_run_id, "approved"):
                logger.info("auto-approved CM-level gate")
                return
            time.sleep(0.5)
        logger.warning("gate auto-approver timed out without firing")

    threading.Thread(target=_auto_approve_when_gated, daemon=True).start()

    events = list(run_cm(goal=GOAL, meta_run_id=meta_run_id))

    print("\n" + "=" * 60)
    print(f"CM RUN EVENT STREAM ({len(events)} events)")
    print("=" * 60)
    for ev in events:
        print(f"  [{ev['type']:<22}] run_id={ev['run_id'][:8]}… iter={ev['iteration']} payload={_short_payload(ev)}")

    type_counts = Counter(ev["type"] for ev in events)
    print("\nType counts:", dict(type_counts))

    # ---- Structural asserts ------------------------------------------------
    assert type_counts["cm_plan_started"] == 1, type_counts
    assert type_counts["cm_plan"] == 1, type_counts
    assert type_counts["cm_candidates"] == 1, type_counts
    assert type_counts["cm_synthesis"] == 1, type_counts

    # Pull the cm_candidates envelope so we know how many sub-runs to expect.
    cm_candidates_ev = next(ev for ev in events if ev["type"] == "cm_candidates")
    candidate_list = cm_candidates_ev["payload"]["candidates"]
    n_candidates = len(candidate_list)
    print(f"\nn_candidates from cm_candidates: {n_candidates}")

    # 1:1 — one cm_candidate_complete per candidate, always emitted.
    assert type_counts["cm_candidate_complete"] == n_candidates, (
        f"expected {n_candidates} cm_candidate_complete events, got {type_counts['cm_candidate_complete']}"
    )
    assert type_counts["cm_candidate_start"] == n_candidates, (
        f"expected {n_candidates} cm_candidate_start events, got {type_counts['cm_candidate_start']}"
    )

    # Terminal meta run_complete: find the one whose run_id == meta_run_id.
    meta_completes = [
        ev for ev in events
        if ev["type"] == "run_complete" and ev["run_id"] == meta_run_id
    ]
    assert len(meta_completes) == 1, (
        f"expected exactly 1 meta run_complete with run_id={meta_run_id}, got {len(meta_completes)}"
    )
    meta_complete = meta_completes[0]
    assert meta_complete["payload"]["meta_run_id"] == meta_run_id
    assert meta_complete["payload"]["total_candidates"] == n_candidates, meta_complete["payload"]
    assert events[-1] is meta_complete, "meta run_complete must be the last event"

    # ---- Zero-candidate path: check the synthesis stub ---------------------
    if n_candidates == 0:
        synth = next(ev for ev in events if ev["type"] == "cm_synthesis")
        md = synth["payload"]["markdown"]
        assert md.startswith("## Pattern"), md[:80]
        assert "No reviews matched" in md, md[:200]
        print("\nZero-candidate path verified.")

    # ---- is_demo provenance: spot-check that forwarded events carry meta_run_id + candidate_index ----
    if n_candidates > 0:
        forwarded_sub_events = [
            ev for ev in events
            if ev["type"] in ("node_start", "state_update", "retrieval_step",
                              "human_gate_open", "run_complete", "error")
            and ev["run_id"] != meta_run_id
        ]
        assert forwarded_sub_events, "expected forwarded sub-run events for non-empty batch"
        for ev in forwarded_sub_events:
            p = ev.get("payload") or {}
            assert p.get("meta_run_id") == meta_run_id, (
                f"forwarded event missing meta_run_id={meta_run_id}: {ev}"
            )
            assert isinstance(p.get("candidate_index"), int), (
                f"forwarded event missing candidate_index: {ev}"
            )
        print(f"Verified {len(forwarded_sub_events)} forwarded sub-run events carry meta_run_id + candidate_index.")

    # ---- Audit log: sub-runs tagged is_demo=1 ------------------------------
    # Pull the sub_run_ids from forwarded run_complete events and check the DB.
    sub_run_ids = sorted({
        ev["run_id"] for ev in events
        if ev["type"] == "run_complete" and ev["run_id"] != meta_run_id
    })
    if sub_run_ids:
        placeholders = ",".join("?" * len(sub_run_ids))
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = [
                dict(r) for r in conn.execute(
                    f"SELECT run_id, review_id, is_demo, stop_reason FROM audit_log "
                    f"WHERE run_id IN ({placeholders})",
                    sub_run_ids,
                ).fetchall()
            ]
        print(f"\naudit_log rows for {len(sub_run_ids)} sub_run_ids:")
        for r in rows:
            print(f"  run_id={r['run_id'][:8]}… review_id={r['review_id']} "
                  f"is_demo={r['is_demo']} stop_reason={r['stop_reason']!r}")
        for r in rows:
            assert r["is_demo"] == 1, (
                f"sub-run {r['run_id']} not tagged is_demo=1: {r}"
            )

    print("\n" + "=" * 60)
    print("✓ All CM assertions passed.")
    print("=" * 60)


if __name__ == "__main__":
    test_cm()
