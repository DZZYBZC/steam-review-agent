"""
evals/_recalibrate_rescore.py — Iter3 Phase 3 offline rescore helper.

Reads the frozen run JSON (run_20260408_111204.json) + recalibrated golden.json,
runs every scorer over the frozen records (no LLM calls for the judges thanks
to disk caching), writes a snapshot marked _RECALIBRATED, and verifies that
every NON-retrieval aggregate is byte-identical to the pre-recalibration
snapshot (snapshot_20260408_111306.json).

This is deliberately a standalone helper, not a flag on run_evals.py — it
must never share a codepath with real eval runs, because its whole contract
is "no agent execution, no behavior drift."

Usage: python evals/_recalibrate_rescore.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run_evals import load_cases
from evals.scorers.deterministic import score_records
from evals.scorers.gating_accuracy import gating_accuracy_batch
from evals.scorers.judge_action import judge_action_batch
from evals.scorers.judge_grounding import judge_grounding_batch
from evals.scorers.pairwise import pairwise_batch
from evals.snapshot import SNAPSHOTS_DIR, write_snapshot

FROZEN_RUN = Path(__file__).resolve().parent / "runs" / "run_20260408_111204.json"
BASELINE_SNAPSHOT = SNAPSHOTS_DIR / "snapshot_20260408_111306.json"

# Every aggregate field that MUST NOT change (i.e., everything except
# retrieval.* and schema_version). This is the Phase-3 guardrail.
GUARDED_PATHS: list[str] = [
    "n_records",
    "n_scored",
    "stop_reasons",
    "action.n_correct",
    "action.n_evaluable",
    "action.n_excluded_total",
    "action.n_excluded_infra_error",
    "action.n_excluded_no_response",
    "action.correct_rate",
    "citation.subset_ok_rate",
    "grounding.n_hard_violations",
    "diagnostics.low_conf_with_cite_flag_count",
    "judge.low_conf_with_cite.n_flagged",
    "judge.low_conf_with_cite.n_honest_hedge",
    "judge.low_conf_with_cite.n_misleading_fix_claim",
    "judge.low_conf_with_cite.n_unclear",
    "judge.action.n_flagged",
    "judge.action.n_over_escalation",
    "judge.action.n_missed_escalation",
    "judge.action.n_category_drift",
    "judge.action.n_tolerable_disagreement",
    "judge.action.n_judge_error",
    "judge.pairwise.n_judged",
    "judge.pairwise.n_revision_improved",
    "judge.pairwise.n_revision_neutral",
    "judge.pairwise.n_revision_regressed",
    "judge.pairwise.n_judge_error",
    "judge.pairwise.n_deterministic",
    "critic_health.approval_rate_overall",
    "critic_health.iter0_approval",
    "critic_health.mean_iters_to_approval",
    "critic_health.rejections_by_reason_type",
    "gating.accuracy",
    "gating.false_skip_rate",
    "gating.false_retrieve_rate",
    "gating.confusion",
    "cost.total_tokens",
    "failure_modes",
]


def _get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def main() -> None:
    assert FROZEN_RUN.exists(), f"Missing frozen run: {FROZEN_RUN}"
    assert BASELINE_SNAPSHOT.exists(), f"Missing baseline snapshot: {BASELINE_SNAPSHOT}"

    with FROZEN_RUN.open() as f:
        run = json.load(f)
    records = run["records"]
    cases = load_cases()

    # Score the frozen records through the NEW scorer pipeline. Judges read
    # from disk cache (same run_file_basename) — no LLM calls fire.
    basename = FROZEN_RUN.stem
    scored = score_records(cases, records)
    gating = gating_accuracy_batch(cases, records)
    judge = judge_grounding_batch(cases, records, scored, run_file_basename=basename)
    judge_action = judge_action_batch(cases, records, scored, run_file_basename=basename)
    pairwise = pairwise_batch(cases, records, run_file_basename=basename)

    print(
        f"Judge cache hits: grounding={judge['n_from_cache']}/{judge['n_flagged']}, "
        f"action={judge_action['n_from_cache']}/{judge_action['n_flagged']}, "
        f"pairwise={pairwise['n_from_cache']}/{pairwise['n_judged']}"
    )

    # Write the recalibrated snapshot, then rename with a _RECALIBRATED suffix
    # so provenance is unmistakable in the snapshots/ dir listing.
    out = write_snapshot(
        scored, gating, judge, judge_action, pairwise,
        records=records,
        run_file=FROZEN_RUN,
        filters={"recalibrated": True, "frozen_run": FROZEN_RUN.name},
    )
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    renamed = out.with_name(f"snapshot_{ts}_RECALIBRATED.json")
    out.rename(renamed)
    print(f"Wrote: {renamed.name}")

    # ---- Byte-identity guardrail ----
    with BASELINE_SNAPSHOT.open() as f:
        baseline = json.load(f)
    with renamed.open() as f:
        current = json.load(f)

    base_agg = baseline["aggregates"]
    curr_agg = current["aggregates"]

    violations: list[str] = []
    for path in GUARDED_PATHS:
        b = _get(base_agg, path)
        c = _get(curr_agg, path)
        if b != c:
            violations.append(f"  {path}: {b!r} -> {c!r}")

    print()
    print("=" * 70)
    print("PHASE-3 BYTE-IDENTITY GUARDRAIL")
    print("=" * 70)
    if violations:
        print(f"FAIL — {len(violations)} non-retrieval field(s) moved:")
        for line in violations:
            print(line)
        sys.exit(1)
    else:
        print(f"PASS — all {len(GUARDED_PATHS)} guarded fields byte-identical to baseline.")

    # Print the retrieval deltas explicitly (the ONLY block that should move).
    print()
    print("RETRIEVAL DELTAS (expected to change):")
    base_ret = base_agg.get("retrieval", {})
    curr_ret = curr_agg.get("retrieval", {})
    all_keys = sorted(set(base_ret) | set(curr_ret))
    for k in all_keys:
        b = base_ret.get(k, "(missing)")
        c = curr_ret.get(k, "(missing)")
        marker = "  " if b == c else "* "
        print(f"  {marker}{k:<40} {b} -> {c}")

    print()
    print(f"Schema version: {baseline.get('schema_version')} -> {current.get('schema_version')}")


if __name__ == "__main__":
    main()
