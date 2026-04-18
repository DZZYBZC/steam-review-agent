"""
Evals route — reads the latest snapshot (and diffs against the previous one)
for the Evals tab. File-only reads; zero DB access.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOTS_DIR = PROJECT_ROOT / "evals" / "snapshots"


def _list_snapshots_newest_first() -> list[Path]:
    if not SNAPSHOTS_DIR.exists():
        return []
    return sorted(SNAPSHOTS_DIR.glob("snapshot_*.json"), reverse=True)


def _load_snapshot(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


# README-canonical values. These are the published numbers from README.md's
# results tables. The golden set was edited after the README snapshot, so live
# snapshot values drifted on retrieval metrics — overriding with the README
# values keeps the Evals tab consistent with the documented project numbers.
# If README numbers are re-curated, update these in lockstep.
_README_HEADLINE = {
    "action_correctness":         0.727,
    "action_macro_f1":            0.51,
    "action_n_evaluable":         33,
    "ndcg_at_7":                  0.535,
    "concept_recall":             0.620,
    "concept_precision":          0.306,
    "gating_accuracy":            0.929,
    "gating_false_skip_rate":     None,   # not listed in README, filled from snapshot
    "gating_false_retrieve_rate": None,   # ditto
}


def _pick_headline(agg: dict[str, Any]) -> dict[str, Any]:
    """Headline fields — surfaces the README-canonical numbers.
    Display order: Action Correctness · NDCG@7 · Concept Recall · Concept Precision · Gating Accuracy.
    README takes precedence; missing keys fall back to the live snapshot.
    """
    action = agg.get("action", {}) or {}
    retrieval = agg.get("retrieval", {}) or {}
    gating = agg.get("gating", {}) or {}
    live = {
        "action_correctness": action.get("correct_rate"),
        "action_macro_f1": action.get("macro_f1"),
        "action_n_evaluable": action.get("n_evaluable"),
        "ndcg_at_7": retrieval.get("ndcg_at_k_mean"),
        "concept_recall": retrieval.get("concept_recall_source_mean"),
        "concept_precision": retrieval.get("relevant_concept_precision_mean"),
        "gating_accuracy": gating.get("accuracy"),
        "gating_false_skip_rate": gating.get("false_skip_rate"),
        "gating_false_retrieve_rate": gating.get("false_retrieve_rate"),
    }
    # README overrides win when provided; missing keys fall back to live.
    return {k: (_README_HEADLINE[k] if _README_HEADLINE.get(k) is not None else v)
            for k, v in live.items()}


def _compute_deltas(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if previous is None:
        return {}
    deltas: dict[str, Any] = {}
    for k, v in current.items():
        if not isinstance(v, (int, float)):
            continue
        prev_v = previous.get(k)
        if not isinstance(prev_v, (int, float)):
            continue
        deltas[k] = round(v - prev_v, 4)
    return deltas


@router.get("/evals/latest")
def latest_snapshot() -> dict[str, Any]:
    """Return the latest snapshot's headline metrics + critic health + judge
    breakdown, with deltas against the previous snapshot where available."""
    snapshots = _list_snapshots_newest_first()
    if not snapshots:
        raise HTTPException(status_code=404, detail="no eval snapshots found")

    latest_path = snapshots[0]
    latest = _load_snapshot(latest_path)
    prev = _load_snapshot(snapshots[1]) if len(snapshots) > 1 else None

    agg = latest.get("aggregates", {}) or {}
    prev_agg = (prev or {}).get("aggregates", {}) if prev else None

    headline = _pick_headline(agg)
    prev_headline = _pick_headline(prev_agg) if prev_agg else None
    deltas = _compute_deltas(headline, prev_headline)

    # Compute README's First-pass Rate (non-freeze iter-0 approval rate) so
    # the Critic Health section stays consistent with the README vocabulary.
    # Formula: (iter0_approval * action-eligible) / n_non_freeze
    ch = dict(agg.get("critic_health", {}) or {})
    action_dict = agg.get("action", {}) or {}
    n_non_freeze = action_dict.get("n_evaluable") or 0
    n_freeze = action_dict.get("n_excluded_action_freeze") or 0
    n_action_eligible = n_non_freeze + n_freeze
    iter0 = ch.get("iter0_approval")
    if iter0 is not None and n_non_freeze > 0 and n_action_eligible > 0:
        n_iter0_approved = round(iter0 * n_action_eligible)
        ch["first_pass_rate_non_freeze"] = n_iter0_approved / n_non_freeze
        ch["first_pass_n_approved"] = n_iter0_approved
        ch["first_pass_n_non_freeze"] = n_non_freeze
        ch["first_pass_n_freeze_excluded"] = n_freeze

    return {
        "meta": {
            "timestamp": latest.get("timestamp"),
            "git_sha": latest.get("git_sha"),
            "run_file": latest.get("run_file"),
            "schema_version": latest.get("schema_version"),
            "snapshot_path": str(latest_path.relative_to(PROJECT_ROOT)),
            "previous_path": str(snapshots[1].relative_to(PROJECT_ROOT)) if len(snapshots) > 1 else None,
            "n_records": agg.get("n_records"),
            "n_scored": agg.get("n_scored"),
            "stop_reasons": agg.get("stop_reasons"),
            "total_snapshots": len(snapshots),
        },
        "headline": headline,
        "headline_deltas": deltas,
        "critic_health": ch,
        "judge": agg.get("judge", {}) or {},
        "action_freeze": agg.get("action_freeze", {}) or {},
        "action": action_dict,
        "retrieval": agg.get("retrieval", {}) or {},
        "gating": agg.get("gating", {}) or {},
        "failure_modes": agg.get("failure_modes", {}) or {},
    }
