"""
evals/snapshot.py — Versioned eval snapshots (M5 Step 8).

A snapshot is a small JSON file that captures the aggregate metrics from one
eval run, plus the git SHA of the code that produced them. Two snapshots
taken at different commits can be diffed to answer "did this change move
the numbers, and in which direction?"

Files land in evals/snapshots/snapshot_<timestamp>.json (gitignored — see
D7 for the snapshots_archive/ pattern used at milestones).

Public surface:
  - write_snapshot(scored, gating, judge, judge_action, pairwise, records, run_file, filters) -> Path
  - load_latest_snapshot(exclude=None) -> dict | None
  - diff_snapshots(prev, curr) -> list[str]   # one line per changed metric
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"

# Bump when the snapshot aggregates dict shape changes in a non-additive way
# (field renamed, removed, or semantics changed). Visible in the snapshot
# payload and in the diff print path so transitions are interpretable.
# See git log for the per-version history.
SNAPSHOT_SCHEMA_VERSION = 6

# Metrics surfaced in the snapshot summary AND used for the one-line diff.
# Order is preserved in the diff output. Each entry is a dotted path into
# the snapshot["aggregates"] dict (resolved by _get_path).
DIFF_METRICS: list[tuple[str, str, str]] = [
    # (label, dotted_path, format_spec)
    ("action_correct_rate",          "action.correct_rate",                ".3f"),
    # Schema v6: recall_at_k_mean (which conflated raw-retriever output with
    # investigator filtering) was split into source vs relevant recall, plus
    # companion concept hit-rates and a filter-drop diagnostic.
    ("recall_source_mean",           "retrieval.recall_source_mean",       ".3f"),
    ("recall_relevant_mean",         "retrieval.recall_relevant_mean",     ".3f"),
    # Layered retrieval Phase A1: concept_recall is the formula generalization
    # of retrieval_recall over named concepts. Post the 1:1 mechanical
    # migration, these are byte-equal to recall_source_mean / recall_relevant_mean.
    # Phase A2 manual annotation will let them legitimately diverge.
    ("concept_recall_source_mean",    "retrieval.concept_recall_source_mean",     ".3f"),
    ("concept_recall_postfilter_mean","retrieval.concept_recall_postfilter_mean", ".3f"),
    ("n_concept_unannotated_eligible","retrieval.n_concept_unannotated_eligible", "d"),
    ("concept_hit_source_rate",      "retrieval.concept_hit_source_rate",  ".3f"),
    ("concept_hit_relevant_rate",    "retrieval.concept_hit_relevant_rate",".3f"),
    ("n_lost_to_filter",             "retrieval.n_lost_to_filter",         "d"),
    ("citation_subset_ok_rate",      "citation.subset_ok_rate",            ".3f"),
    # Schema v2: grounding.compliant_rate (a rate that mixed two different
    # things) replaced by an explicit count of HARD violations only, plus a
    # parallel diagnostic count for the low_conf_with_cite flag.
    ("grounding_band_hard_violations","grounding.n_hard_violations",       "d"),
    ("low_conf_with_cite_flag_count","diagnostics.low_conf_with_cite_flag_count", "d"),
    # Schema v3: LLM judge rulings on the low_conf_with_cite flagged cases.
    ("judge_lc_honest_hedge",        "judge.low_conf_with_cite.n_honest_hedge",         "d"),
    ("judge_lc_misleading_fix_claim","judge.low_conf_with_cite.n_misleading_fix_claim", "d"),
    ("judge_lc_unclear",             "judge.low_conf_with_cite.n_unclear",              "d"),
    # Schema v4: LLM judge rulings on the wrong_action_severity flagged cases.
    # judge_error is in the diff so any future regression in judge infra
    # (model deprecation, schema drift, prompt-driven parse failure) shows up
    # as movement in this row rather than hiding inside a substantive bucket.
    ("judge_act_over_escalation",       "judge.action.n_over_escalation",       "d"),
    ("judge_act_missed_escalation",     "judge.action.n_missed_escalation",     "d"),
    ("judge_act_category_drift",        "judge.action.n_category_drift",        "d"),
    ("judge_act_tolerable_disagreement","judge.action.n_tolerable_disagreement","d"),
    ("judge_act_judge_error",           "judge.action.n_judge_error",           "d"),
    # Schema v5: pairwise revision-improvement judge rulings. judge_pw_judge_error
    # belongs in DIFF_METRICS for the same reason as the action equivalent —
    # infrastructure misfires must be visible immediately. judge_pw_n_deterministic
    # belongs because a jump/drop in the shortcut count is itself a signal:
    # a drop means the responder is changing more drafts; a spike means it's
    # becoming a no-op revision-loop tax.
    ("judge_pw_revision_improved",   "judge.pairwise.n_revision_improved",   "d"),
    ("judge_pw_revision_neutral",    "judge.pairwise.n_revision_neutral",    "d"),
    ("judge_pw_revision_regressed",  "judge.pairwise.n_revision_regressed",  "d"),
    ("judge_pw_judge_error",         "judge.pairwise.n_judge_error",         "d"),
    ("judge_pw_n_judged",            "judge.pairwise.n_judged",              "d"),
    ("judge_pw_n_deterministic",     "judge.pairwise.n_deterministic",       "d"),
    ("critic_approval_overall",      "critic_health.approval_rate_overall",".3f"),
    ("critic_iter0_approval",        "critic_health.iter0_approval",       ".3f"),
    ("action_override_runs",         "action_freeze.n_runs_with_action_override", "d"),
    ("effective_iter0_rate",         "action_freeze.effective_iter0_rate",  ".3f"),
    ("gating_accuracy",              "gating.accuracy",                    ".3f"),
    ("gating_false_skip_rate",       "gating.false_skip_rate",             ".3f"),
    ("gating_false_retrieve_rate",   "gating.false_retrieve_rate",         ".3f"),
    ("total_tokens",                 "cost.total_tokens",                  ",d"),
]


# ---------- Git helpers ----------------------------------------------------

def _git_sha() -> str:
    """Return current HEAD sha. 'unstaged' if working tree is dirty."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "no_git"

    try:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return sha

    return f"{sha[:12]}+unstaged" if dirty else sha[:12]


# ---------- Judge aggregates (schema v3 + v4) -----------------------------

def _build_judge_grounding_block(judge: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build the `low_conf_with_cite` sub-block of the judge aggregate. If the
    grounding judge was not run (judge is None), return zero counts so the
    schema shape is stable across runs — downstream diff readers prefer
    "0/0/0" over missing keys.
    """
    if judge is None:
        rulings: dict[str, int] = {}
        n_flagged = 0
    else:
        rulings = judge.get("rulings", {}) or {}
        n_flagged = judge.get("n_flagged", 0)
    return {
        "n_flagged":              n_flagged,
        "n_honest_hedge":         rulings.get("honest_hedge", 0),
        "n_misleading_fix_claim": rulings.get("misleading_fix_claim", 0),
        "n_unclear":              rulings.get("unclear", 0),
    }


def _build_judge_action_block(judge_action: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build the `action` sub-block of the judge aggregate (schema v4). Mirrors
    _build_judge_grounding_block. n_judge_error is reported alongside the
    four semantic buckets so judge infrastructure misfires are visible
    immediately and never get silently absorbed into a substantive ruling.
    """
    if judge_action is None:
        rulings: dict[str, int] = {}
        n_flagged = 0
    else:
        rulings = judge_action.get("rulings", {}) or {}
        n_flagged = judge_action.get("n_flagged", 0)
    return {
        "n_flagged":                n_flagged,
        "n_over_escalation":        rulings.get("over_escalation", 0),
        "n_missed_escalation":      rulings.get("missed_escalation", 0),
        "n_category_drift":         rulings.get("category_drift", 0),
        "n_tolerable_disagreement": rulings.get("tolerable_disagreement", 0),
        "n_judge_error":            rulings.get("judge_error", 0),
    }


def _build_pairwise_block(pairwise: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build the `pairwise` sub-block of the judge aggregate (schema v5). Mirrors
    the other two judge blocks. n_judge_error stays isolated. n_deterministic
    is the count of revision_neutral rulings that came from the deterministic
    normalize-equal shortcut (a subset of n_revision_neutral, NOT a separate
    bucket — surfaced so the snapshot diff can track shortcut firing rate
    over time).
    """
    if pairwise is None:
        rulings: dict[str, int] = {}
        n_judged = 0
        n_deterministic = 0
    else:
        rulings = pairwise.get("rulings", {}) or {}
        n_judged = pairwise.get("n_judged", 0)
        n_deterministic = pairwise.get("n_deterministic", 0)
    return {
        "n_judged":             n_judged,
        "n_revision_improved":  rulings.get("revision_improved", 0),
        "n_revision_neutral":   rulings.get("revision_neutral", 0),
        "n_revision_regressed": rulings.get("revision_regressed", 0),
        "n_judge_error":        rulings.get("judge_error", 0),
        "n_deterministic":      n_deterministic,
    }


# ---------- Aggregate builder ---------------------------------------------

def _build_aggregates(
    scored: dict[str, Any],
    gating: dict[str, Any],
    judge: dict[str, Any] | None,
    judge_action: dict[str, Any] | None,
    pairwise: dict[str, Any] | None,
    records: list[dict],
) -> dict[str, Any]:
    """
    Roll the scorer outputs into a flat dict of comparable numbers.
    These are the values diffed across snapshots.
    """
    per_case = scored["per_case"]
    n_scored = len(per_case)

    # Action — exclude cases that died on infrastructure errors (llm_error,
    # parse_error). They're missing decisions, not wrong ones, and shouldn't
    # poison the snapshot diff.
    action_evaluable = [
        s["action_correctness"] for s in per_case.values()
        if s["action_correctness"].get("applicable", True)
    ]
    n_action_evaluable = len(action_evaluable)
    n_action_excluded = n_scored - n_action_evaluable
    n_correct = sum(1 for s in action_evaluable if s["correct"])
    action_correct_rate = (n_correct / n_action_evaluable) if n_action_evaluable else None

    # Split exclusions for snapshot diffs.
    excluded_action = [
        s["action_correctness"] for s in per_case.values()
        if not s["action_correctness"].get("applicable", True)
    ]
    n_excluded_infra = sum(
        1 for s in excluded_action
        if s.get("stop_reason") in {"llm_error", "parse_error"}
    )
    n_excluded_no_response = sum(
        1 for s in excluded_action
        if s.get("stop_reason") == "no_response_needed"
    )

    # Retrieval (schema v6): slot-based recall with source vs relevant pools.
    # Cases with empty must_include are not_applicable and excluded entirely.
    # Cases where the gate skipped retrieval (source_ids empty) are surfaced
    # as a separate count and excluded from the recall/concept-hit means —
    # they're a gating failure mode, not a retrieval failure mode.
    retrieval_scored = [
        s["retrieval_recall"] for s in per_case.values()
        if not s["retrieval_recall"].get("not_applicable")
    ]
    n_gate_false_skip = sum(1 for s in retrieval_scored if s.get("gate_false_skip"))
    eligible = [s for s in retrieval_scored if not s.get("gate_false_skip")]

    def _mean(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 3) if vals else None

    recall_source_mean = _mean([s["recall_source"] for s in eligible])
    recall_relevant_mean = _mean([s["recall_relevant"] for s in eligible])
    concept_hit_source_rate = _mean([1.0 if s["concept_hit_source"] else 0.0 for s in eligible])
    concept_hit_relevant_rate = _mean([1.0 if s["concept_hit_relevant"] else 0.0 for s in eligible])
    # Chunks the retriever surfaced but the investigator's Self-RAG filter dropped.
    n_lost_to_filter = sum(len(s.get("dropped_by_filter") or []) for s in eligible)
    n_cases_with_drops = sum(1 for s in eligible if s.get("dropped_by_filter"))

    # Concept recall (Phase A1). Same eligibility rules as retrieval_recall:
    # not_applicable cases are excluded entirely; gate_false_skip cases are
    # excluded from the mean. After the 1:1 mechanical migration the
    # eligible-set and means must be identical to retrieval_recall above.
    concept_scored = [
        s["concept_recall"] for s in per_case.values()
        if not s["concept_recall"].get("not_applicable")
    ]
    concept_eligible = [s for s in concept_scored if not s.get("gate_false_skip")]
    concept_recall_source_mean = _mean([s["recall_source"] for s in concept_eligible])
    concept_recall_postfilter_mean = _mean([s["recall_postfilter"] for s in concept_eligible])
    # Annotation-coverage gauge: cases that are retrieval-eligible (have
    # must_include_chunk_ids) but lack required_concepts. Post-A1 mechanical
    # migration this should be 0; if it ever climbs, the migration didn't
    # cover a newly-added case and concept recall silently shrinks.
    n_concept_unannotated_eligible = sum(
        1 for s in per_case.values()
        if not s["retrieval_recall"].get("not_applicable")
        and s["concept_recall"].get("not_applicable")
    )

    # Citation subset
    n_cite_ok = sum(1 for s in per_case.values() if s["citation_audit"]["subset_ok"])
    cite_rate = (n_cite_ok / n_scored) if n_scored else None

    # Grounding band — schema v2: count hard violations explicitly, and
    # count low_conf_with_cite as a separate diagnostic flag (not a failure).
    n_hard_violations = sum(
        1 for s in per_case.values()
        if s["grounding_band_compliance"].get("hard_violation")
    )
    n_low_conf_with_cite_flag = sum(
        1 for s in per_case.values()
        if s["grounding_band_compliance"].get("flag") == "low_conf_with_cite"
    )

    # Token cost
    total_tokens = sum(s["token_cost"]["total"] for s in per_case.values())

    # Critic health: pull a couple of headline numbers up to top level for diff
    ch = scored["batch"]["critic_health"]
    iter0 = ch["approval_rate_by_iteration"].get(0)

    # Stop reasons
    by_stop: dict[str, int] = defaultdict(int)
    for r in records:
        if r.get("ok"):
            by_stop[(r["result"] or {}).get("stop_reason") or "(none)"] += 1
        else:
            by_stop["(harness_error)"] += 1

    # Failure-mode tally (deterministic). Schema v2: low_conf_with_cite is a
    # diagnostic flag, not a failure — counted in the diagnostics dict below.
    failure_modes: dict[str, int] = defaultdict(int)
    for s in per_case.values():
        cite = s["citation_audit"]
        if cite["out_of_set_ids"]:
            failure_modes["cited_irrelevant_patch"] += 1
        hv = s["grounding_band_compliance"].get("hard_violation")
        if hv:
            failure_modes[hv] += 1
        act = s["action_correctness"]
        # Skip infra-error cases — no predicted action to be wrong about.
        if act.get("applicable", True) and not act["correct"] and act["ideal"] and act["predicted"]:
            failure_modes["wrong_action_severity"] += 1

    # Action-freeze metrics (Iter7): computed from serialized result records.
    n_runs_with_action_override = sum(
        1 for r in records
        if r.get("ok") and (r.get("result") or {}).get("action_override_count", 0) > 0
    )
    # effective_iter0_rate: fraction of eligible runs that reached human_approval
    # after their first critic evaluation (by normal approval OR coordinator override).
    # Denominator: non-error runs that reached the critic at least once.
    _eligible_for_eff = [
        r for r in records
        if r.get("ok")
        and (r.get("result") or {}).get("stop_reason") not in {"llm_error", "parse_error", "no_response_needed"}
    ]
    _n_eff_denom = len(_eligible_for_eff)
    # Numerator: runs where either (a) critic approved at iter0 (iteration_count==1
    # means only one responder pass happened) or (b) coordinator overrode at iter0
    # (first_override_at_iteration==1, set once and never overwritten).
    _n_eff_numer = sum(
        1 for r in _eligible_for_eff
        if (
            (r.get("result") or {}).get("first_override_at_iteration", -1) == 1
            or (r.get("result") or {}).get("iteration_count", 0) == 1
        )
    )
    effective_iter0_rate = round(_n_eff_numer / _n_eff_denom, 3) if _n_eff_denom else None

    return {
        "n_records": len(records),
        "n_scored": n_scored,
        "stop_reasons": dict(by_stop),
        "action": {
            "n_correct": n_correct,
            "n_evaluable": n_action_evaluable,
            "n_excluded_total": n_action_excluded,
            "n_excluded_infra_error": n_excluded_infra,
            "n_excluded_no_response": n_excluded_no_response,
            "correct_rate": action_correct_rate,
        },
        "retrieval": {
            "recall_source_mean": recall_source_mean,
            "recall_relevant_mean": recall_relevant_mean,
            "concept_hit_source_rate": concept_hit_source_rate,
            "concept_hit_relevant_rate": concept_hit_relevant_rate,
            "n_with_must_include": len(retrieval_scored),
            "n_eligible_for_recall": len(eligible),
            "n_gate_false_skip_in_recall_pool": n_gate_false_skip,
            "n_lost_to_filter": n_lost_to_filter,
            "n_cases_with_filter_drops": n_cases_with_drops,
            "concept_recall_source_mean": concept_recall_source_mean,
            "concept_recall_postfilter_mean": concept_recall_postfilter_mean,
            "n_concept_eligible_for_recall": len(concept_eligible),
            "n_concept_unannotated_eligible": n_concept_unannotated_eligible,
        },
        "citation": {
            "subset_ok_rate": cite_rate,
        },
        "grounding": {
            "n_hard_violations": n_hard_violations,
        },
        "diagnostics": {
            "low_conf_with_cite_flag_count": n_low_conf_with_cite_flag,
        },
        "judge": {
            "low_conf_with_cite": _build_judge_grounding_block(judge),
            "action":             _build_judge_action_block(judge_action),
            "pairwise":           _build_pairwise_block(pairwise),
        },
        "critic_health": {
            "approval_rate_overall": ch["approval_rate_overall"],
            "iter0_approval": iter0,
            "mean_iters_to_approval": ch["mean_iterations_to_approval"],
            "rejections_by_reason_type": ch["rejections_by_reason_type"],
        },
        "action_freeze": {
            "n_runs_with_action_override": n_runs_with_action_override,
            "effective_iter0_rate": effective_iter0_rate,
        },
        "gating": {
            "accuracy": gating["accuracy"],
            "false_skip_rate": gating["false_skip_rate"],
            "false_retrieve_rate": gating["false_retrieve_rate"],
            "confusion": gating["confusion"],
        },
        "cost": {
            "total_tokens": total_tokens,
        },
        "failure_modes": dict(failure_modes),
    }


# ---------- Read / write --------------------------------------------------

def write_snapshot(
    scored: dict[str, Any],
    gating: dict[str, Any],
    judge: dict[str, Any] | None,
    judge_action: dict[str, Any] | None,
    pairwise: dict[str, Any] | None,
    records: list[dict],
    run_file: Path | str,
    filters: dict,
) -> Path:
    """Write a snapshot file and return its path."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "timestamp": timestamp,
        "git_sha": _git_sha(),
        "run_file": str(run_file),
        "filters": filters,
        "aggregates": _build_aggregates(scored, gating, judge, judge_action, pairwise, records),
    }
    out = SNAPSHOTS_DIR / f"snapshot_{timestamp}.json"
    with out.open("w") as f:
        json.dump(payload, f, indent=2)
    return out


def load_latest_snapshot(exclude: Path | None = None) -> dict | None:
    """
    Return the most recent snapshot dict, or None if no snapshots exist.
    `exclude` lets the caller skip the file just written so we compare
    against the previous one rather than against ourselves.
    """
    if not SNAPSHOTS_DIR.exists():
        return None
    candidates = sorted(SNAPSHOTS_DIR.glob("snapshot_*.json"))
    if exclude is not None:
        candidates = [p for p in candidates if p != exclude]
    if not candidates:
        return None
    with candidates[-1].open() as f:
        return json.load(f)


def _get_path(d: dict, dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _fmt(v: Any, spec: str) -> str:
    if v is None:
        return "n/a"
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def diff_snapshots(prev: dict, curr: dict) -> list[str]:
    """
    Return one-line-per-metric diff strings between two snapshots.
    Skips metrics that are unchanged or both None.
    """
    prev_agg = prev.get("aggregates", {})
    curr_agg = curr.get("aggregates", {})
    lines: list[str] = []

    # Schema version transition annotation. Missing field means v1 (pre-v2
    # snapshots had no schema_version key). Surface a one-line note at the
    # top of the diff so the field renames/replacements that follow are
    # interpretable rather than mysterious.
    prev_v = prev.get("schema_version", 1)
    curr_v = curr.get("schema_version", 1)
    if prev_v != curr_v:
        # Per-transition annotations. New entries get added as the schema
        # bumps; older transitions remain interpretable forever.
        annotations = {
            (1, 2): "grounding metric split: low_conf_with_cite is now a flag, not a violation",
            (2, 3): "judge layer added: low_conf_with_cite cases now ruled by LLM",
            (1, 3): "grounding metric split (v1→v2) AND judge layer added (v2→v3)",
            (3, 4): "judge_action layer added: wrong_action_severity cases now ruled by LLM",
            (2, 4): "judge_grounding (v2→v3) AND judge_action (v3→v4) layers added",
            (1, 4): "grounding metric split (v1→v2), judge_grounding (v2→v3), AND judge_action (v3→v4) layers added",
            (4, 5): "pairwise revision-improvement judge added: multi-iteration approved cases now ruled by LLM (with deterministic normalize-equal shortcut)",
            (3, 5): "judge_action (v3→v4) AND pairwise revision-improvement (v4→v5) layers added",
            (2, 5): "judge_grounding (v2→v3), judge_action (v3→v4), AND pairwise (v4→v5) layers added",
            (1, 5): "grounding metric split (v1→v2), judge_grounding (v2→v3), judge_action (v3→v4), AND pairwise (v4→v5) layers added",
            (5, 6): "retrieval block split into source vs relevant recall, concept hit-rate companion added, false-skip cases separated, lost-to-filter diagnostic added, recall_at_k_mean removed (was a conflation)",
            (4, 6): "pairwise (v4→v5) AND retrieval recalibration (v5→v6)",
            (3, 6): "judge_action (v3→v4), pairwise (v4→v5), AND retrieval recalibration (v5→v6)",
            (2, 6): "judge_grounding (v2→v3), judge_action (v3→v4), pairwise (v4→v5), AND retrieval recalibration (v5→v6)",
            (1, 6): "full schema evolution (v1→v6)",
        }
        note = annotations.get((prev_v, curr_v), "schema shape changed")
        lines.append(
            f"  ⚠ schema_version changed: {prev_v} → {curr_v} ({note})"
        )

    for label, path, spec in DIFF_METRICS:
        p = _get_path(prev_agg, path)
        c = _get_path(curr_agg, path)
        if p is None and c is None:
            continue
        if p == c:
            continue
        # Compute delta if both numeric
        delta_str = ""
        if isinstance(p, (int, float)) and isinstance(c, (int, float)):
            delta = c - p
            sign = "+" if delta >= 0 else ""
            try:
                delta_str = f"  ({sign}{format(delta, spec)})"
            except (TypeError, ValueError):
                delta_str = f"  ({sign}{delta})"
        lines.append(f"  {label:<32} {_fmt(p, spec):>12} -> {_fmt(c, spec):>12}{delta_str}")
    return lines
